"""The lease heartbeat (WORKFLOW_DESIGN.md §6): a running job renews its lease, so an LLM
call longer than the lease is not reaped and run twice."""

import asyncio

import pytest

from adapters.jobs.memory import MemoryJobStore
from domain.models import ReviewResult
from domain.ports import ReviewRequest
from tests.conftest import log_events
from tests.worker.test_process import (
    GOOD_DIFF,
    FakeGitProvider,
    claim,
    enqueue,
    make_context,
    make_payload,
)
from tests.worker.test_review_stage import FakePublisher
from worker.process import ReviewStage, process_claimed_job

TICK = 0.01


class LeaseStore(MemoryJobStore):
    """Counts renewals; can fail them (Postgres away) or report the lease lost."""

    def __init__(self) -> None:
        super().__init__()
        self.renewals = 0
        self.failing = False
        self.lost = False

    async def extend_lease(self, job_id: str, *, worker_id: str) -> bool:
        self.renewals += 1
        if self.failing:
            raise ConnectionError("database restarting")
        if self.lost:
            return False
        return await super().extend_lease(job_id, worker_id=worker_id)


class SlowGateway:
    """An LLM call that lasts ``ticks`` heartbeats; ``during`` runs as it starts."""

    def __init__(self, ticks: int, during: object = None) -> None:
        self.ticks = ticks
        self.during = during

    async def review(self, request: ReviewRequest) -> ReviewResult:
        if callable(self.during):
            self.during()
        await asyncio.sleep(self.ticks * TICK)
        return ReviewResult(summary="Fine.", findings=(), provider="p", model="m")


async def run(store: LeaseStore, gateway: SlowGateway, publisher: FakePublisher) -> bool:
    await enqueue(store, make_payload())
    return await process_claimed_job(
        await claim(store),
        FakeGitProvider(make_context(GOOD_DIFF)),
        store,
        review=ReviewStage(gateway=gateway, publisher=publisher),
        lease_renewal_seconds=TICK,
    )


async def test_a_slow_llm_call_keeps_its_lease() -> None:
    store = LeaseStore()

    assert await run(store, SlowGateway(ticks=10), FakePublisher()) is True

    # The two checkpoints (before the LLM call and before posting), plus the heartbeat.
    assert store.renewals > 2 + 3


async def test_the_heartbeat_stops_with_the_job() -> None:
    store = LeaseStore()
    await run(store, SlowGateway(ticks=3), FakePublisher())
    after = store.renewals

    await asyncio.sleep(5 * TICK)

    assert store.renewals == after


async def test_a_failed_renewal_is_logged_and_tried_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = LeaseStore()

    def postgres_away() -> None:
        store.failing = True
        asyncio.get_running_loop().call_later(5 * TICK, setattr, store, "failing", False)

    done = await run(store, SlowGateway(ticks=10, during=postgres_away), FakePublisher())

    failures = log_events(caplog, "job.lease.renew_failed")
    assert failures and {f.error_type for f in failures} == {"ConnectionError"}
    assert "database restarting" not in caplog.text
    # Postgres came back before posting: the job finished.
    assert done is True


async def test_a_lost_lease_still_stops_the_run_before_posting() -> None:
    store = LeaseStore()
    publisher = FakePublisher()

    done = await run(
        store, SlowGateway(ticks=5, during=lambda: setattr(store, "lost", True)), publisher
    )

    assert done is False
    assert publisher.calls == []
