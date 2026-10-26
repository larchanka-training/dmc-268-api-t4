"""LISTEN review_jobs: the latency optimisation, never the correctness path.

docs/db_models_and_migrations.md §6: ``LISTEN``/``NOTIFY`` requires a
dedicated asyncpg connection outside the SQLAlchemy pool, and a missed
notification must never strand a job — the worker's poll-interval fallback
is the correctness source (docs/WORKFLOW_DESIGN.md §1). This coroutine's
whole job is to turn notifications into ``wake.set()``.

asyncpg (0.32) exposes no ``connection.notifies`` queue: notifications
arrive through ``add_listener`` callbacks and connection loss through a
termination listener, so the loop below is built on those instead.
"""

import asyncio
import contextlib
import logging

import asyncpg  # type: ignore[import-untyped]

from adapters.db.repository import NOTIFY_CHANNEL
from worker.periodic import ConnectionFactory

#: Pause before reconnecting after a lost or failed connection. The
#: listener is latency-only, so a fixed 1s pause is always safe.
RECONNECT_PAUSE_SECONDS = 1.0


async def listen_for_jobs(
    dsn: str,
    wake: asyncio.Event,
    *,
    logger: logging.Logger | None = None,
    connect: ConnectionFactory | None = None,
) -> None:
    """Long-running: relay ``NOTIFY review_jobs`` to ``wake.set()``.

    Runs until cancelled. Never raises: connection failures are logged
    with the exception class only — the message could echo the DSN, which
    carries credentials (AGENTS.md hard rule 2) — and retried after
    RECONNECT_PAUSE_SECONDS. A run of failures logs one warning when it
    starts and one info when it ends, not one line per retry: the retry
    runs every second, and the database being down is "Target down"'s alert
    (docs/observability.md § Levels). Cancellation propagates and closes
    the connection; correctness never depends on this coroutine at all.
    ``connect`` is the test seam, as in worker.periodic.
    """
    log = logger if logger is not None else logging.getLogger(__name__)
    open_connection = connect if connect is not None else asyncpg.connect
    failing = False
    while True:
        connection = None
        try:
            connection = await open_connection(dsn)
            if failing:
                log.info("worker.listener.restored")
                failing = False
            await _listen_until_lost(connection, wake, log)
            # Reached only when the server dropped the connection.
            log.warning("worker.listener.lost")
        except asyncio.CancelledError:
            raise
        except Exception as err:
            if not failing:
                log.warning("worker.listener.failed", extra={"error_type": type(err).__name__})
                failing = True
        finally:
            if connection is not None:
                with contextlib.suppress(Exception):
                    await connection.close()
        await asyncio.sleep(RECONNECT_PAUSE_SECONDS)


async def _listen_until_lost(
    connection: asyncpg.Connection, wake: asyncio.Event, log: logging.Logger
) -> None:
    """Register the listener and park until the connection terminates.

    Split out of the reconnect loop so the callbacks close over locals of
    this call frame, not over loop variables.
    """
    lost = asyncio.Event()
    connection.add_termination_listener(lambda _connection: lost.set())
    await connection.add_listener(NOTIFY_CHANNEL, lambda *args: wake.set())
    log.info("worker.listener.ready", extra={"channel": NOTIFY_CHANNEL})
    # A termination listener fires for loss and for close alike; nothing
    # else closes this connection, so this returns only on loss.
    await lost.wait()
