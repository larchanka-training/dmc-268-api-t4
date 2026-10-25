"""Periodic-task leadership (advisory lock) and the lease reaper tick.

docs/BACKEND_ARCHITECTURE.md § Processes (periodic tasks): they run inside
the worker, they are singletons while claiming is not, and each tick takes
a Postgres advisory lock so N workers never double-run it. The lock is
session-scoped, so it is released automatically when the connection dies —
a worker crashing mid-tick frees it rather than wedging every other worker.

The reaper itself is the recovery path of docs/WORKFLOW_DESIGN.md §6: jobs
in PROCESSING whose ``locked_until`` has passed are moved to RETRYING (or
FAILED on exhaustion) by ``PostgresJobStore.reap_expired``; this module
decides who runs the tick and when.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

import asyncpg  # type: ignore[import-untyped]

from adapters.db.repository import PostgresJobStore

#: One stable int per periodic task. The values are arbitrary — only their
#: uniqueness matters, since they all live in one 64-bit advisory-lock
#: namespace shared by every periodic task in every worker.
TASK_KEYS: dict[str, int] = {
    # 0x72656170 spells "reap" in ASCII; any stable constant will do.
    "reaper": 0x72656170,
}

#: How a tick opens its connection; injectable so tests never touch a socket
#: they do not own. Matches ``asyncpg.connect``.
ConnectionFactory = Callable[[str], Awaitable[asyncpg.Connection]]


async def run_if_leader(
    conn: asyncpg.Connection, task_key: int, task: Callable[[], Awaitable[None]]
) -> bool:
    """The advisory-lock gate of docs/BACKEND_ARCHITECTURE.md § Processes:
    whoever wins ``pg_try_advisory_lock`` runs this tick, everyone else
    skips it. The try/finally unlocks on the way out; the lock also dies
    with the connection, so a crashed holder cannot wedge the task.

    Returns True when the task ran, False when the lock was already held.
    """
    if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", task_key):
        return False
    try:
        await task()
    finally:
        await conn.fetchval("SELECT pg_advisory_unlock($1)", task_key)
    return True


async def reap_tick(
    store: PostgresJobStore,
    task_key: int,
    dsn: str,
    *,
    connect: ConnectionFactory | None = None,
    logger: logging.Logger | None = None,
) -> int | None:
    """One reaper pass under the advisory lock.

    Connection strategy: a dedicated ``asyncpg.connect`` per tick, not a
    SQLAlchemy pool connection. ``pg_try_advisory_lock`` is session-scoped,
    so the lock's lifetime must be exactly the tick's: a pooled connection
    would carry the lock across ticks, while a dedicated connection both
    bounds it and guarantees the crash case releases it.

    Returns the reaped count, or None when another worker holds the lock
    this tick (that worker's reap is this tick's reap).
    """
    log = logger if logger is not None else logging.getLogger(__name__)
    open_connection = connect if connect is not None else asyncpg.connect
    connection = await open_connection(dsn)
    reaped = 0

    async def _reap() -> None:
        nonlocal reaped
        reaped = await store.reap_expired()

    try:
        leader = await run_if_leader(connection, task_key, _reap)
    finally:
        with contextlib.suppress(Exception):
            await connection.close()
    if not leader:
        return None
    # Count only: ids and kinds are metadata, everything else on a job row
    # is untrusted webhook data.
    log.info("worker.reaper.tick", extra={"reaped": reaped})
    return reaped


async def reaper_loop(
    store: PostgresJobStore,
    dsn: str,
    stop: asyncio.Event,
    *,
    interval: float,
    connect: ConnectionFactory | None = None,
    logger: logging.Logger | None = None,
) -> None:
    """Run reap_tick every ``interval`` seconds until ``stop`` is set.

    A failed tick (database down) is retried on the next interval — the
    reap is a conditional UPDATE, so a late tick loses nothing. A run of
    failed ticks logs one warning when it starts and one info when it
    ends; the database being down is "Target down"'s alert
    (docs/observability.md § Levels). ``stop`` interrupts the interval
    pause promptly.
    """
    log = logger if logger is not None else logging.getLogger(__name__)
    failing = False
    while not stop.is_set():
        try:
            await reap_tick(store, TASK_KEYS["reaper"], dsn, connect=connect, logger=log)
        except Exception as err:
            if not failing:
                log.warning("worker.reaper.failed", extra={"error_type": type(err).__name__})
                failing = True
        else:
            if failing:
                log.info("worker.reaper.recovered")
                failing = False
        sleeper = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({sleeper}, timeout=interval)
        finally:
            sleeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sleeper
