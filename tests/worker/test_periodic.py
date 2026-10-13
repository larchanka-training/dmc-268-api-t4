"""run_if_leader: the advisory-lock gate, on real PostgreSQL.

Advisory locks are session-scoped, so these tests use distinct fresh
connections per case; the lock is chosen per test module to stay out of
the reaper key's namespace.
"""

import os

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from adapters.db.dsn import to_asyncpg
from tests.db.support import COMPOSE_URL, require_postgres
from worker.periodic import run_if_leader

TASK_KEY = 0x54455374  # "TESt"; anything stable and unused elsewhere


@pytest.fixture(scope="module")
def pg_dsn() -> str:
    base = make_url(os.environ.get("DATABASE_URL") or COMPOSE_URL)
    require_postgres(base, reason="the advisory-lock (periodic task) tests need a local PostgreSQL")
    return to_asyncpg(base)


async def test_the_winner_runs_the_task_and_the_unlock_releases_it(
    pg_dsn: str,
) -> None:
    first = await asyncpg.connect(pg_dsn)
    second = await asyncpg.connect(pg_dsn)
    try:
        ran: list[bool] = []

        async def task() -> None:
            ran.append(True)

        assert await run_if_leader(first, TASK_KEY, task) is True
        assert ran == [True]

        # The finally unlocked it: another connection wins immediately.
        assert await run_if_leader(second, TASK_KEY, task) is True
        assert ran == [True, True]
    finally:
        await first.close()
        await second.close()


async def test_a_second_connection_skips_while_the_lock_is_held(pg_dsn: str) -> None:
    holder = await asyncpg.connect(pg_dsn)
    contender = await asyncpg.connect(pg_dsn)
    try:
        assert await holder.fetchval("SELECT pg_try_advisory_lock($1)", TASK_KEY)
        ran: list[bool] = []

        async def task() -> None:
            ran.append(True)

        assert await run_if_leader(contender, TASK_KEY, task) is False
        assert ran == []

        assert await holder.fetchval("SELECT pg_advisory_unlock($1)", TASK_KEY)
        assert await run_if_leader(contender, TASK_KEY, task) is True
        assert ran == [True]
    finally:
        await holder.close()
        await contender.close()
