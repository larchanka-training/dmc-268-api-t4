"""The worker claim loop: poll fallback, LISTEN wake, graceful drain.

docs/WORKFLOW_DESIGN.md §2 Step 4 (claim) and §1 (a 5-second poll is the
fallback so a missed notification only costs latency, never correctness);
docs/BACKEND_ARCHITECTURE.md § Processes (one process, worker_concurrency
asyncio tasks, each claiming independently thanks to SKIP LOCKED) and
§ Deployment (on SIGTERM the worker stops claiming and finishes what it
is holding).
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from adapters.db.dsn import to_asyncpg
from adapters.db.job_events import PostgresStepRecorder
from adapters.db.repository import PostgresJobStore
from adapters.db.session import build_engine
from domain.ports import GitProvider, JobRepository, StepRecorder
from worker.config import DEFAULT_POLL_INTERVAL_SECONDS, WorkerSettings
from worker.listener import listen_for_jobs
from worker.periodic import reaper_loop
from worker.process import ReviewStage, process_claimed_job

#: The production listener plus any test fake: takes the asyncpg DSN and
#: the shared wake event, runs until cancelled.
ListenerFactory = Callable[[str, asyncio.Event], Coroutine[Any, Any, None]]


async def claim_and_process_once(
    store: JobRepository,
    vcs: GitProvider,
    worker_id: str,
    *,
    review: ReviewStage | None = None,
    steps: StepRecorder | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    """One claim attempt (WORKFLOW_DESIGN.md §2 Step 4); process if won.

    Returns True when a job was claimed and run to its outcome — completed,
    failed or retried; process_claimed_job logs which — and False when the
    store had nothing to hand this worker. The worker loop's drain step
    and the tests both call this directly.
    """
    claimed = await store.claim(worker_id)
    if claimed is None:
        return False
    log = logger if logger is not None else logging.getLogger(__name__)
    await process_claimed_job(claimed, vcs, store, review=review, steps=steps, logger=log)
    return True


async def worker_loop(
    store: JobRepository,
    vcs: GitProvider,
    worker_id: str,
    wake: asyncio.Event,
    stop: asyncio.Event,
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    review: ReviewStage | None = None,
    steps: StepRecorder | None = None,
    logger: logging.Logger | None = None,
) -> None:
    """One concurrency slot's loop (docs/BACKEND_ARCHITECTURE.md § Processes).

    Drain claims while work is available, then wait on wake OR stop with a
    ``poll_interval`` timeout: the listener (or any enqueue's NOTIFY) sets
    ``wake`` for sub-second pickup, and the timeout is the poll fallback —
    a missed notification costs latency only
    (docs/db_models_and_migrations.md §6). ``stop`` breaks immediately and
    is checked between claims, so shutdown never starts a new review.
    Cancellation (run_worker teardown) propagates cleanly.
    """
    log = logger if logger is not None else logging.getLogger(__name__)
    while not stop.is_set():
        while not stop.is_set():
            try:
                processed = await claim_and_process_once(
                    store, vcs, worker_id, review=review, steps=steps, logger=log
                )
            except Exception as err:
                # A store error (database restarting) must not kill the
                # slot: log it, fall back to the timed wait, and the next
                # tick retries. Cancellation is not Exception, so it
                # propagates.
                log.error(
                    "claim attempt failed: worker_id=%s exc_class=%s",
                    worker_id,
                    type(err).__name__,
                )
                break
            if not processed:
                break
        if stop.is_set():
            break
        wake.clear()
        await _await_either(wake, stop, poll_interval)


async def run_worker(
    settings: WorkerSettings,
    vcs: GitProvider,
    *,
    logger: logging.Logger | None = None,
    stop: asyncio.Event | None = None,
    listener: ListenerFactory | None = None,
    engine: AsyncEngine | None = None,
    store: JobRepository | None = None,
    review: ReviewStage | None = None,
) -> None:
    """Run one worker process until ``stop`` is set, then drain and exit.

    Own engine and event loop (docs/WORKFLOW_DESIGN.md §8: a separate
    process, so API restarts never kill an in-flight review) with
    ``worker_concurrency`` claim slots, one LISTEN connection and one
    periodic reaper. ``engine`` and ``store`` are the test seams
    (tests/worker/test_loop.py): an injected engine only records the
    dispose ordering, and an injected non-PostgreSQL store — the in-memory
    one — has no lease table, so no reaper connection is opened for it.

    Graceful shutdown (docs/BACKEND_ARCHITECTURE.md § Deployment: "On
    SIGTERM the worker stops claiming and finishes what it is holding"):
    ``stop`` interrupts every slot's wait and the reaper's pause, the
    listener is cancelled, each slot finishes the job it holds and claims
    nothing new, then the engine is disposed. Cancellation of this
    coroutine itself runs the same teardown.
    """
    log = logger if logger is not None else logging.getLogger(__name__)
    stop = stop if stop is not None else asyncio.Event()
    wake = asyncio.Event()
    engine = build_engine(settings.db) if engine is None else engine
    jobs = PostgresJobStore(engine) if store is None else store
    # Steps go to the same database as the queue; the in-memory store has no table.
    steps = PostgresStepRecorder(engine) if isinstance(jobs, PostgresJobStore) else None
    dsn = to_asyncpg(settings.db.database_url)
    listen = listener if listener is not None else listen_for_jobs
    slots = [
        asyncio.create_task(
            worker_loop(
                jobs,
                vcs,
                _slot_worker_id(index),
                wake,
                stop,
                poll_interval=settings.poll_interval_seconds,
                review=review,
                steps=steps,
                logger=log,
            ),
            name=f"worker-loop-{index}",
        )
        for index in range(settings.worker_concurrency)
    ]
    listener_task = asyncio.create_task(listen(dsn, wake), name="job-listener")
    # The reaper recovers expired leases via PostgresJobStore.reap_expired;
    # any other store (the injected in-memory one) has no leases to reap,
    # so the advisory-lock tick must not open a connection for it.
    reaper_task: asyncio.Task[None] | None = None
    if isinstance(jobs, PostgresJobStore):
        reaper_task = asyncio.create_task(
            reaper_loop(jobs, dsn, stop, interval=settings.reaper_interval_seconds, logger=log),
            name="lease-reaper",
        )
    stopped = asyncio.create_task(stop.wait(), name="worker-stop")
    try:
        await stopped
    finally:
        stop.set()
        stopped.cancel()
        # Reap the cancelled stop-waiter so it is never a pending task.
        with contextlib.suppress(asyncio.CancelledError):
            await stopped
        listener_task.cancel()
        # Drain in-flight work: each slot finishes the job it holds and
        # claims nothing new; no slot is cancelled mid-review.
        await asyncio.gather(*slots, return_exceptions=True)
        background = [listener_task] if reaper_task is None else [listener_task, reaper_task]
        await asyncio.gather(*background, return_exceptions=True)
        await engine.dispose()


def _slot_worker_id(index: int) -> str:
    """Stable within a slot for the process's lifetime; the random suffix
    keeps two worker processes from sharing a lease identity."""
    return f"worker-{index}-{uuid.uuid4().hex[:8]}"


async def _await_either(wake: asyncio.Event, stop: asyncio.Event, timeout: float) -> None:
    """Return as soon as ``wake`` or ``stop`` is set, or after ``timeout``.

    The per-event waits run as child tasks cancelled in ``finally``, so
    this helper never consumes an event, leaks a task, or blocks a
    cancellation of its caller — no sleep-polling anywhere.
    """
    tasks = [asyncio.ensure_future(wake.wait()), asyncio.ensure_future(stop.wait())]
    try:
        await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
