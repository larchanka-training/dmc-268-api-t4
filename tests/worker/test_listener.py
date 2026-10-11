"""listen_for_jobs against real PostgreSQL: a dedicated LISTEN connection
that turns notifications into wake events.

The LISTEN connection needs no tables, so these tests run against the
resolved server's default database — no scratch database, no Alembic.
Readiness is a bounded notify-until-heard handshake: PostgreSQL drops a
notification sent before LISTEN registers, so the test re-sends until the
listener answers; every wait is event-driven with a generous 2s
``wait_for`` bound, never a sleep-based assertion.
"""

import asyncio
import os

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from adapters.db.dsn import to_asyncpg
from adapters.db.repository import NOTIFY_CHANNEL
from tests.db.support import COMPOSE_URL, require_postgres
from worker.listener import listen_for_jobs


@pytest.fixture(scope="module")
def pg_dsn() -> str:
    base = make_url(os.environ.get("DATABASE_URL") or COMPOSE_URL)
    require_postgres(base, reason="the LISTEN/NOTIFY listener tests need a local PostgreSQL")
    return to_asyncpg(base)


async def _notify_until_heard(sender: asyncpg.Connection, wake: asyncio.Event) -> None:
    """Re-send the notification until the listener has registered its
    LISTEN; each dropped send is free, and the whole handshake is bounded
    by the caller's wait_for."""
    while not wake.is_set():
        await sender.execute(f"SELECT pg_notify('{NOTIFY_CHANNEL}', '')")
        await asyncio.sleep(0.02)


async def _cancel_and_join(listener: asyncio.Task) -> None:
    listener.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(listener, timeout=1.0)


async def test_a_notification_sets_the_wake_event(pg_dsn: str) -> None:
    wake = asyncio.Event()
    listener = asyncio.create_task(listen_for_jobs(pg_dsn, wake))
    try:
        sender = await asyncpg.connect(pg_dsn)
        try:
            await asyncio.wait_for(_notify_until_heard(sender, wake), timeout=2.0)
        finally:
            await sender.close()
        assert wake.is_set()
    finally:
        await _cancel_and_join(listener)


async def test_the_listener_stays_up_across_notifications_until_cancelled(
    pg_dsn: str,
) -> None:
    wake = asyncio.Event()
    listener = asyncio.create_task(listen_for_jobs(pg_dsn, wake))
    try:
        sender = await asyncpg.connect(pg_dsn)
        try:
            await asyncio.wait_for(_notify_until_heard(sender, wake), timeout=2.0)
            wake.clear()
            # The same connection must still be listening, not a corpse.
            await asyncio.wait_for(_notify_until_heard(sender, wake), timeout=2.0)
        finally:
            await sender.close()
    finally:
        await _cancel_and_join(listener)
