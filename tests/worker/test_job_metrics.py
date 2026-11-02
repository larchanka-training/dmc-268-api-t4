"""The worker's job metrics from worker/process.py (docs/observability.md § Application):
one jobs_finished_total per attempt, the review duration from enqueue to a terminal
status, claim latency from when the job became claimable, and the in-flight gauge.

The in-memory store runs on a step clock, so queue wait is exact and nothing sleeps.
The registry is process-wide: each test compares values before and after.
"""

from adapters.jobs.memory import MemoryJobStore
from adapters.observability import metrics
from domain.errors import ForgeUnavailableError, InstallationGoneError
from domain.ports import PullRequestContext
from tests.conftest import FixedClock
from tests.worker.test_process import (
    GOOD_DIFF,
    FakeGitProvider,
    claim,
    enqueue,
    make_context,
    make_payload,
)
from worker.process import process_claimed_job

REGISTRY = metrics.WORKER_REGISTRY


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def finished(status: str, error_kind: str = "") -> float:
    return sample("jobs_finished_total", status=status, error_kind=error_kind)


async def test_a_completed_job_observes_enqueue_to_completion() -> None:
    clock = FixedClock()
    store = MemoryJobStore(clock=clock)
    await enqueue(store, make_payload())
    clock.advance(90)
    claimed = await claim(store)
    done = finished("COMPLETED")
    total = sample("review_duration_seconds_sum", status="COMPLETED")
    within_two_minutes = sample("review_duration_seconds_bucket", status="COMPLETED", le="120.0")
    within_a_minute = sample("review_duration_seconds_bucket", status="COMPLETED", le="60.0")

    assert await process_claimed_job(claimed, FakeGitProvider(make_context(GOOD_DIFF)), store)

    assert finished("COMPLETED") == done + 1
    # 90 s in the queue, then this run's own (sub-second) time.
    assert 90 <= sample("review_duration_seconds_sum", status="COMPLETED") - total < 91
    assert (
        sample("review_duration_seconds_bucket", status="COMPLETED", le="120.0")
        == within_two_minutes + 1
    )
    assert (
        sample("review_duration_seconds_bucket", status="COMPLETED", le="60.0") == within_a_minute
    )


async def test_claim_latency_counts_from_when_the_job_became_claimable() -> None:
    clock = FixedClock()
    store = MemoryJobStore(clock=clock)
    await enqueue(store, make_payload())
    clock.advance(7)
    claimed = await claim(store)
    total = sample("job_claim_latency_seconds_sum")
    within_ten = sample("job_claim_latency_seconds_bucket", le="10.0")
    within_five = sample("job_claim_latency_seconds_bucket", le="5.0")

    await process_claimed_job(claimed, FakeGitProvider(make_context(GOOD_DIFF)), store)

    assert sample("job_claim_latency_seconds_sum") - total == 7
    assert sample("job_claim_latency_seconds_bucket", le="10.0") == within_ten + 1
    assert sample("job_claim_latency_seconds_bucket", le="5.0") == within_five


async def test_a_retry_is_counted_but_is_no_review_duration() -> None:
    store = MemoryJobStore(clock=FixedClock())
    await enqueue(store, make_payload())
    claimed = await claim(store)
    retried = finished("RETRYING", "forge_unavailable")
    durations = [
        sample("review_duration_seconds_count", status=status) for status in ("COMPLETED", "FAILED")
    ]
    down = FakeGitProvider(error=ForgeUnavailableError("github", "connection reset"))

    await process_claimed_job(claimed, down, store)

    assert finished("RETRYING", "forge_unavailable") == retried + 1
    assert [
        sample("review_duration_seconds_count", status=status) for status in ("COMPLETED", "FAILED")
    ] == durations


async def test_a_failed_job_is_counted_with_its_error_kind_and_duration() -> None:
    store = MemoryJobStore(clock=FixedClock())
    await enqueue(store, make_payload())
    claimed = await claim(store)
    failed = finished("FAILED", "installation_removed")
    timed = sample("review_duration_seconds_count", status="FAILED")
    gone = FakeGitProvider(error=InstallationGoneError(42, "uninstalled"))

    await process_claimed_job(claimed, gone, store)

    assert finished("FAILED", "installation_removed") == failed + 1
    assert sample("review_duration_seconds_count", status="FAILED") == timed + 1


class GaugeReadingGitProvider(FakeGitProvider):
    """Reads worker_in_flight while the job runs."""

    async def fetch_pull_request(
        self, installation_id: int, repo_full_name: str, number: int
    ) -> PullRequestContext:
        self.in_flight = sample("worker_in_flight")
        return await super().fetch_pull_request(installation_id, repo_full_name, number)


async def test_the_in_flight_gauge_holds_the_running_job_only() -> None:
    store = MemoryJobStore(clock=FixedClock())
    await enqueue(store, make_payload())
    claimed = await claim(store)
    idle = sample("worker_in_flight")
    vcs = GaugeReadingGitProvider(make_context(GOOD_DIFF))

    await process_claimed_job(claimed, vcs, store)

    assert vcs.in_flight == idle + 1
    assert sample("worker_in_flight") == idle
