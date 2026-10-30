"""The worker's retry loops log a run of failures once, not once per retry.

docs/observability.md § Levels: while Postgres is down the LISTEN connection
retries every second, each claim slot every poll interval and the reaper every
tick. One warning when a run of failures starts and one info when it ends keeps
an outage from flooding the logs and Sentry; "Target down" is the alert. No test
touches the network or a database: every connection is a local fake, and every
wait is an event wait bounded by ``wait_for``.
"""

import asyncio
import logging
from typing import Any

import pytest

from adapters.jobs.memory import MemoryJobStore
from tests.worker.test_loop import FlakyClaimStore, _wait_completed
from tests.worker.test_process import (
    GOOD_DIFF,
    FakeGitProvider,
    enqueue,
    make_context,
    make_payload,
)
from worker import listener as listener_module
from worker.listener import listen_for_jobs
from worker.loop import worker_loop
from worker.periodic import reaper_loop

FAILURES = 3


def _count(caplog: pytest.LogCaptureFixture, level: int, text: str) -> int:
    return sum(1 for r in caplog.records if r.levelno == level and text in r.getMessage())


def _failing_connect(connection: Any) -> tuple[Any, list[int]]:
    """A connect() that refuses FAILURES times, then hands out ``connection``."""
    attempts = [0]

    async def connect(dsn: str) -> Any:
        attempts[0] += 1
        if attempts[0] <= FAILURES:
            raise OSError("connection refused")
        return connection

    return connect, attempts


async def test_claim_failures_log_one_warning_and_one_recovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    store = FlakyClaimStore(MemoryJobStore(), failures=FAILURES)
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    await enqueue(store, make_payload(delivery_id="d-1", pr_number=7))
    wake, stop = asyncio.Event(), asyncio.Event()

    # poll_interval=0: a failed claim is retried on the loop's next turn.
    loop_task = asyncio.create_task(worker_loop(store, vcs, "w-0", wake, stop, poll_interval=0))
    try:
        await _wait_completed(store, 1)
        stop.set()
        await asyncio.wait_for(loop_task, timeout=1.0)
    finally:
        loop_task.cancel()

    assert store.claim_attempts > FAILURES
    assert _count(caplog, logging.WARNING, "worker.claim.failed") == 1
    assert _count(caplog, logging.INFO, "worker.claim.recovered") == 1
    assert _count(caplog, logging.ERROR, "worker.claim.failed") == 0


class _ListeningConnection:
    """The asyncpg calls the listener makes; signals once it is listening."""

    def __init__(self) -> None:
        self.listening = asyncio.Event()

    def add_termination_listener(self, callback: Any) -> None:
        pass

    async def add_listener(self, channel: str, callback: Any) -> None:
        self.listening.set()

    async def close(self) -> None:
        pass


async def test_listener_failures_log_one_warning_and_one_recovery(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(listener_module, "RECONNECT_PAUSE_SECONDS", 0.0)
    connection = _ListeningConnection()
    connect, attempts = _failing_connect(connection)

    task = asyncio.create_task(
        listen_for_jobs("postgresql://fake", asyncio.Event(), connect=connect)
    )
    try:
        await asyncio.wait_for(connection.listening.wait(), timeout=1.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert attempts[0] == FAILURES + 1
    assert _count(caplog, logging.WARNING, "worker.listener.failed") == 1
    assert _count(caplog, logging.INFO, "worker.listener.restored") == 1
    assert _count(caplog, logging.ERROR, "worker.listener.failed") == 0


class _LockConnection:
    """Wins every advisory lock: the only queries a reaper tick makes."""

    async def fetchval(self, query: str, *args: object) -> bool:
        return True

    async def close(self) -> None:
        pass


class _ReapStore:
    def __init__(self) -> None:
        self.reaped = asyncio.Event()

    async def reap_expired(self) -> int:
        self.reaped.set()
        return 0

    async def record_periodic_run(self, task: str, interval_seconds: float) -> None:
        return None


async def test_reaper_failures_log_one_warning_and_one_recovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    store = _ReapStore()
    stop = asyncio.Event()
    connect, attempts = _failing_connect(_LockConnection())

    task = asyncio.create_task(
        reaper_loop(store, "postgresql://fake", stop, interval=0, connect=connect)  # type: ignore[arg-type]
    )
    try:
        await asyncio.wait_for(store.reaped.wait(), timeout=1.0)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)
    finally:
        task.cancel()

    assert attempts[0] == FAILURES + 1
    assert _count(caplog, logging.WARNING, "worker.reaper.failed") == 1
    assert _count(caplog, logging.INFO, "worker.reaper.recovered") == 1
    assert _count(caplog, logging.ERROR, "worker.reaper.failed") == 0
