"""worker.loop on the in-memory store: claim-once, the slot loop's drain /
wake / stop behaviour, and retry timing through the injected clock.

All fakes are local or reused from tests/worker/test_process.py; nothing
touches the network and no test sleeps — waits are event waits with a
generous 1s ``wait_for`` bound.

Determinism note: MemoryJobStore never suspends (its lock is uncontended),
so a slot task that has drained N jobs is already parked inside
``_await_either`` by the time the test observes the Nth completion —
setting ``wake`` after that observation can never be raced away by the
loop's ``wake.clear()``.
"""

import asyncio
import random
from datetime import UTC, datetime, timedelta

from adapters.db.config import DBSettings
from adapters.jobs.memory import MemoryJobStore
from domain.errors import ForgeUnavailableError
from domain.ports import (
    ClaimedJob,
    JobRepository,
    JobStats,
    NewReviewJob,
    ReviewJob,
    ReviewJobStatus,
)
from tests.conftest import log_events
from tests.worker.test_process import (
    GOOD_DIFF,
    FakeGitProvider,
    enqueue,
    make_context,
    make_payload,
)
from worker.config import WorkerSettings
from worker.loop import claim_and_process_once, run_worker, worker_loop

DSN = "postgresql+asyncpg://dmc:dmc@localhost:5432/dmc268"


class StepClock:
    """A domain.ports.Clock fake; advance it instead of sleeping."""

    def __init__(self) -> None:
        self._now = datetime.now(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class SignallingStore:
    """Delegating JobRepository that signals completions and counts claims."""

    def __init__(self, inner: JobRepository) -> None:
        self._inner = inner
        self.completed = asyncio.Event()
        self.completed_count = 0
        self.claim_attempts = 0

    async def enqueue(self, job: NewReviewJob) -> str | None:
        return await self._inner.enqueue(job)

    async def claim(self, worker_id: str) -> ClaimedJob | None:
        self.claim_attempts += 1
        return await self._inner.claim(worker_id)

    async def complete(
        self, job_id: str, stats: JobStats, *, worker_id: str, summary: str | None = None
    ) -> bool:
        done = await self._inner.complete(job_id, stats, worker_id=worker_id, summary=summary)
        if done:
            self.completed_count += 1
            self.completed.set()
        return done

    async def fail(
        self, job_id: str, error_kind: str, *, retryable: bool, worker_id: str
    ) -> ReviewJobStatus | None:
        return await self._inner.fail(job_id, error_kind, retryable=retryable, worker_id=worker_id)

    async def extend_lease(self, job_id: str, *, worker_id: str) -> bool:
        return await self._inner.extend_lease(job_id, worker_id=worker_id)

    async def get(self, job_id: str) -> ReviewJob | None:
        return await self._inner.get(job_id)


class FlakyClaimStore(SignallingStore):
    """Raises from claim() the first N times, then delegates."""

    def __init__(self, inner: JobRepository, failures: int) -> None:
        super().__init__(inner)
        self._failures = failures

    async def claim(self, worker_id: str) -> ClaimedJob | None:
        self.claim_attempts += 1
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("db down")
        return await self._inner.claim(worker_id)


async def _wait_completed(store: SignallingStore, count: int) -> None:
    async def waiter() -> None:
        while store.completed_count < count:
            await store.completed.wait()
            store.completed.clear()

    await asyncio.wait_for(waiter(), timeout=1.0)


async def _status(store: JobRepository, job_id: str) -> ReviewJobStatus:
    job = await store.get(job_id)
    assert job is not None
    return job.status


# --- claim_and_process_once -------------------------------------------------


async def test_claim_and_process_once_completes_a_queued_job() -> None:
    store = MemoryJobStore()
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    job_id = await enqueue(store, make_payload())

    assert await claim_and_process_once(store, vcs, "w-1") is True

    assert await _status(store, job_id) is ReviewJobStatus.COMPLETED
    assert vcs.calls == [(42, "octocat/hello-world", 7)]


async def test_claim_and_process_once_returns_false_when_idle() -> None:
    store = MemoryJobStore()
    vcs = FakeGitProvider(make_context(GOOD_DIFF))

    assert await claim_and_process_once(store, vcs, "w-1") is False
    assert vcs.calls == []


async def test_a_processed_job_counts_even_when_it_fails_retryably() -> None:
    store = MemoryJobStore()
    vcs = FakeGitProvider(error=ForgeUnavailableError("github", "connection reset"))
    job_id = await enqueue(store, make_payload())

    # True: a job was claimed and run to its outcome (RETRYING), not idle.
    assert await claim_and_process_once(store, vcs, "w-1") is True
    assert await _status(store, job_id) is ReviewJobStatus.RETRYING


# --- worker_loop --------------------------------------------------------------


async def test_worker_loop_drains_queued_jobs_then_goes_idle() -> None:
    inner = MemoryJobStore()
    store = SignallingStore(inner)
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    for index in range(3):
        await enqueue(store, make_payload(delivery_id=f"d-{index}", pr_number=7 + index))
    wake, stop = asyncio.Event(), asyncio.Event()

    loop_task = asyncio.create_task(worker_loop(store, vcs, "w-0", wake, stop, poll_interval=60))
    try:
        await _wait_completed(store, 3)

        # Drained every claimable job with one slot, then attempted one
        # more claim (nothing eligible) before parking: 3 wins + 1 idle.
        assert store.claim_attempts == 4
        assert vcs.calls and len(vcs.calls) == 3

        stop.set()
        await asyncio.wait_for(loop_task, timeout=1.0)
    finally:
        loop_task.cancel()


async def test_a_wake_event_interrupts_the_poll_wait_for_a_new_job() -> None:
    inner = MemoryJobStore()
    store = SignallingStore(inner)
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    await enqueue(store, make_payload(delivery_id="d-1", pr_number=7))
    wake, stop = asyncio.Event(), asyncio.Event()

    loop_task = asyncio.create_task(worker_loop(store, vcs, "w-0", wake, stop, poll_interval=60))
    try:
        await _wait_completed(store, 1)

        # The loop is parked in _await_either (see the module docstring):
        # without the wake this job would sit queued for 60s.
        await enqueue(store, make_payload(delivery_id="d-2", pr_number=8))
        wake.set()
        await _wait_completed(store, 2)

        stop.set()
        await asyncio.wait_for(loop_task, timeout=1.0)
    finally:
        loop_task.cancel()


async def test_stop_breaks_the_wait_promptly_without_processing() -> None:
    inner = MemoryJobStore()
    store = SignallingStore(inner)
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    wake, stop = asyncio.Event(), asyncio.Event()

    loop_task = asyncio.create_task(worker_loop(store, vcs, "w-0", wake, stop, poll_interval=60))
    # Let the slot start and park in its wait; then stop must end it long
    # before the 60s interval.
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(loop_task, timeout=1.0)

    assert store.claim_attempts == 1  # the initial idle claim, nothing more
    assert vcs.calls == []


async def test_a_claim_error_is_logged_and_the_slot_survives(caplog) -> None:
    inner = MemoryJobStore()
    store = FlakyClaimStore(inner, failures=1)
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    await enqueue(store, make_payload(delivery_id="d-1", pr_number=7))
    wake, stop = asyncio.Event(), asyncio.Event()

    loop_task = asyncio.create_task(worker_loop(store, vcs, "w-0", wake, stop, poll_interval=60))
    try:
        # One yield is enough for the slot to run its failing claim and
        # park in the wait (MemoryJobStore never suspends; the raise does
        # not either), so the wake below cannot be cleared by the loop.
        await asyncio.sleep(0)
        assert store.claim_attempts == 1

        wake.set()
        await _wait_completed(store, 1)

        stop.set()
        await asyncio.wait_for(loop_task, timeout=1.0)
    finally:
        loop_task.cancel()

    [record] = log_events(caplog, "worker.claim.failed")
    assert record.error_type == "RuntimeError"


# --- run_worker ---------------------------------------------------------------


class EventRecordingStore(SignallingStore):
    """SignallingStore that also logs completion order into ``events``."""

    def __init__(self, inner: JobRepository, events: list[str]) -> None:
        super().__init__(inner)
        self.events = events

    async def complete(
        self, job_id: str, stats: JobStats, *, worker_id: str, summary: str | None = None
    ) -> bool:
        done = await super().complete(job_id, stats, worker_id=worker_id, summary=summary)
        if done:
            self.events.append(f"complete:{job_id}")
        return done


class RecordingEngine:
    """Duck-typed AsyncEngine: with a store injected, run_worker only ever
    calls ``dispose`` on the engine, and this fake logs when that happens."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def dispose(self) -> None:
        self.events.append("dispose")


async def test_run_worker_drains_queued_jobs_then_disposes_the_engine() -> None:
    inner = MemoryJobStore()
    events: list[str] = []
    store = EventRecordingStore(inner, events)
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    first = await enqueue(store, make_payload(delivery_id="d-1", pr_number=7))
    listener_started = asyncio.Event()

    async def silent_listener(dsn: str, wake: asyncio.Event) -> None:
        # Started, but it never notifies: the poll fallback must be what
        # picks up the second job (docs/WORKFLOW_DESIGN.md §1).
        listener_started.set()
        never = asyncio.Event()
        await never.wait()

    settings = WorkerSettings(
        db=DBSettings(database_url=DSN),
        worker_concurrency=1,
        poll_interval_seconds=0.05,
        reaper_interval_seconds=3600.0,
    )
    stop = asyncio.Event()
    run_task = asyncio.create_task(
        run_worker(
            settings,
            vcs,
            stop=stop,
            listener=silent_listener,
            engine=RecordingEngine(events),
            store=store,
        )
    )
    try:
        await asyncio.wait_for(listener_started.wait(), timeout=1.0)
        await _wait_completed(store, 1)

        # The listener never wakes the slot, so only the 50 ms poll
        # fallback can claim the second job — a missed notification costs
        # latency, never correctness.
        second = await enqueue(store, make_payload(delivery_id="d-2", pr_number=8))
        await _wait_completed(store, 2)

        stop.set()
        await asyncio.wait_for(run_task, timeout=1.0)
    finally:
        run_task.cancel()

    assert await _status(store, first) is ReviewJobStatus.COMPLETED
    assert await _status(store, second) is ReviewJobStatus.COMPLETED
    # Drain before dispose (docs/BACKEND_ARCHITECTURE.md § Deployment): both
    # completions are logged ahead of the engine's dispose, which is last.
    assert events.count("dispose") == 1
    assert set(events[:-1]) == {f"complete:{first}", f"complete:{second}"}
    assert events[-1] == "dispose"


# --- retry timing through the clock -------------------------------------------


async def test_a_retrying_job_is_not_reclaimed_until_its_next_attempt() -> None:
    clock = StepClock()
    store = MemoryJobStore(clock=clock, rng=random.Random(0))
    vcs = FakeGitProvider(error=ForgeUnavailableError("github", "connection reset"))
    job_id = await enqueue(store, make_payload())

    assert await claim_and_process_once(store, vcs, "w-1") is True
    stored = await store.get(job_id)
    assert stored is not None
    assert stored.status is ReviewJobStatus.RETRYING
    assert stored.next_attempt_at is not None
    assert stored.next_attempt_at > clock.now()

    # Backoff (1-2 min ± jitter) not yet elapsed: nothing to claim.
    assert await claim_and_process_once(store, vcs, "w-1") is False

    clock.advance(1200)
    assert await claim_and_process_once(store, vcs, "w-1") is True
    stored = await store.get(job_id)
    assert stored is not None
    assert stored.retry_count == 2
