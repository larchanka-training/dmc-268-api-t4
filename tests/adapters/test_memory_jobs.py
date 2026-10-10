"""MemoryJobStore: dedup, supersession, claim/lease/retry, injected clock.

Mirrors the enqueue transaction of docs/WORKFLOW_DESIGN.md §2 Step 3 and the
claim statement of §2 Step 4 — one asyncio lock here stands in for the
single transaction there. Backoff windows come from domain.jobs with a
seeded rng and a stepped clock; no test sleeps.
"""

import random
from datetime import UTC, datetime, timedelta

import pytest

from adapters.jobs.memory import MemoryJobStore
from domain.errors import ForgeUnavailableError
from domain.jobs import DEFAULT_MAX_RETRIES
from domain.ports import ClaimedJob, JobStats, NewReviewJob, ReviewJob, ReviewJobStatus

HEAD_SHA = "a1b2c3d4e5" * 4
BASE_SHA = "f6e5d4c3b2" * 4
OTHER_SHA = "0123456789" * 4


class StepClock:
    """A domain.ports.Clock fake; advance it instead of sleeping."""

    def __init__(self) -> None:
        self._now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def make_job(**overrides: object) -> NewReviewJob:
    values: dict[str, object] = {
        "provider": "github",
        "delivery_id": "d-1",
        "installation_id": 42,
        "repo_id": 751065667,
        "repo_full_name": "octocat/hello-world",
        "pr_number": 7,
        "head_sha": HEAD_SHA,
        "base_sha": BASE_SHA,
        "event_action": "opened",
        "pr_title": "Add a feature",
    }
    values.update(overrides)
    return NewReviewJob(**values)  # type: ignore[arg-type]


def make_stats() -> JobStats:
    return JobStats(
        files_total=2,
        files_reviewable=1,
        hunks_total=2,
        chunks_total=1,
        skipped_counts=(("lockfile", 1),),
    )


async def enqueue(store: MemoryJobStore, job: NewReviewJob) -> str:
    job_id = await store.enqueue(job)
    assert job_id is not None
    return job_id


async def claim(store: MemoryJobStore, worker_id: str = "w-1") -> ClaimedJob:
    claimed = await store.claim(worker_id)
    assert claimed is not None
    return claimed


async def fail_retryable(store: MemoryJobStore, claimed: ClaimedJob) -> None:
    status = await store.fail(
        claimed.job_id, "forge_unavailable", retryable=True, worker_id=claimed.worker_id
    )
    assert status is ReviewJobStatus.RETRYING


# --- enqueue ---------------------------------------------------------------


async def test_enqueue_stores_queued_job_with_payload() -> None:
    store = MemoryJobStore()
    job = make_job()
    job_id = await store.enqueue(job)
    assert job_id
    stored = await store.get(job_id)
    assert stored == ReviewJob(
        job_id=job_id,
        created_at=stored.created_at,
        status=ReviewJobStatus.QUEUED,
        payload=job,
        next_attempt_at=stored.created_at,
    )
    assert stored is not None
    assert stored.created_at.tzinfo is UTC
    assert stored.stats is None
    assert stored.error_kind is None
    assert stored.finished_at is None
    assert stored.retry_count == 0
    # The schema's server_default now(): claimable immediately, never NULL.
    assert stored.next_attempt_at == stored.created_at


async def test_duplicate_delivery_id_same_provider_is_rejected_in_any_status() -> None:
    store = MemoryJobStore()
    assert await store.enqueue(make_job(delivery_id="d-1")) is not None
    second = await store.enqueue(
        make_job(delivery_id="d-2", head_sha=OTHER_SHA)  # supersedes the first
    )
    assert second is not None
    assert await store.enqueue(make_job(delivery_id="d-1")) is None


async def test_same_delivery_id_different_provider_is_accepted() -> None:
    store = MemoryJobStore()
    assert await store.enqueue(make_job(delivery_id="d-1", provider="github")) is not None
    assert await store.enqueue(make_job(delivery_id="d-1", provider="gitlab")) is not None


async def test_null_delivery_id_never_dedups() -> None:
    store = MemoryJobStore()
    assert await store.enqueue(make_job(delivery_id=None, pr_number=7)) is not None
    assert await store.enqueue(make_job(delivery_id=None, pr_number=8)) is not None


# --- supersession ----------------------------------------------------------


async def test_new_job_for_same_pr_supersedes_active_one() -> None:
    store = MemoryJobStore()
    first = await store.enqueue(make_job(delivery_id="d-1"))
    assert first is not None
    second = await store.enqueue(
        make_job(delivery_id="d-2", head_sha=OTHER_SHA, event_action="synchronize")
    )
    assert second is not None

    superseded = await store.get(first)
    assert superseded is not None
    assert superseded.status is ReviewJobStatus.SUPERSEDED
    assert superseded.finished_at is not None
    assert superseded.payload.head_sha == HEAD_SHA

    active = await store.get(second)
    assert active is not None
    assert active.status is ReviewJobStatus.QUEUED
    assert active.finished_at is None


async def test_a_retrying_job_is_still_superseded() -> None:
    store = MemoryJobStore(clock=StepClock())
    first = await enqueue(store, make_job(delivery_id="d-1"))
    claimed = await claim(store)
    await fail_retryable(store, claimed)

    second = await store.enqueue(make_job(delivery_id="d-2", head_sha=OTHER_SHA))
    assert second is not None

    stored = await store.get(first)
    assert stored is not None
    assert stored.status is ReviewJobStatus.SUPERSEDED


async def test_new_job_for_different_pr_leaves_other_jobs_alone() -> None:
    store = MemoryJobStore()
    first = await store.enqueue(make_job(delivery_id="d-1", pr_number=7))
    assert first is not None
    assert await store.enqueue(make_job(delivery_id="d-2", pr_number=8)) is not None

    untouched = await store.get(first)
    assert untouched is not None
    assert untouched.status is ReviewJobStatus.QUEUED
    assert untouched.finished_at is None


async def test_finished_job_is_not_superseded() -> None:
    store = MemoryJobStore()
    job_id = await enqueue(store, make_job(delivery_id="d-1"))
    claimed = await claim(store)
    assert await store.complete(job_id, make_stats(), worker_id=claimed.worker_id)
    assert await store.enqueue(make_job(delivery_id="d-2")) is not None

    stored = await store.get(job_id)
    assert stored is not None
    assert stored.status is ReviewJobStatus.COMPLETED


# --- claim -----------------------------------------------------------------


async def test_claim_returns_the_oldest_job_first() -> None:
    store = MemoryJobStore(clock=StepClock())
    first = await enqueue(store, make_job(delivery_id="d-1", pr_number=7))
    second = await enqueue(store, make_job(delivery_id="d-2", pr_number=8))

    claimed = await claim(store)
    assert claimed.job_id == first
    assert claimed.attempt == 0
    assert claimed.worker_id == "w-1"
    assert claimed.payload.pr_title == "Add a feature"

    next_claimed = await claim(store, "w-2")
    assert next_claimed.job_id == second

    assert await store.claim("w-3") is None


async def test_claim_marks_the_job_processing() -> None:
    store = MemoryJobStore()
    job_id = await enqueue(store, make_job())

    claimed = await claim(store)

    stored = await store.get(job_id)
    assert stored is not None
    assert stored.status is ReviewJobStatus.PROCESSING
    assert claimed.job_id == job_id


async def test_claim_skips_a_job_whose_next_attempt_is_in_the_future() -> None:
    clock = StepClock()
    store = MemoryJobStore(clock=clock, rng=random.Random(7))
    job_id = await enqueue(store, make_job(delivery_id="d-1"))
    claimed = await claim(store, "w-1")
    await fail_retryable(store, claimed)

    retrying = await store.get(job_id)
    assert retrying is not None
    assert retrying.status is ReviewJobStatus.RETRYING
    assert retrying.next_attempt_at is not None
    assert retrying.next_attempt_at > clock.now()

    # Not eligible yet: nothing else is queued.
    assert await store.claim("w-2") is None

    clock.advance(1000)
    reclaimed = await store.claim("w-2")
    assert reclaimed.job_id == job_id
    assert reclaimed.attempt == 1


async def test_claim_is_fair_per_installation() -> None:
    store = MemoryJobStore(clock=StepClock())
    burst = [
        await enqueue(store, make_job(delivery_id=f"d-{index}", pr_number=7 + index))
        for index in range(4)
    ]
    other = await enqueue(
        store,
        make_job(
            delivery_id="d-other",
            installation_id=7,
            pr_number=1,
        ),
    )

    first = await claim(store, "w-1")
    second = await claim(store, "w-2")
    third = await claim(store, "w-3")
    assert first.job_id in burst
    assert second.job_id in burst
    assert third.job_id in burst

    # Installation 42 now runs MAX_IN_FLIGHT_PER_INSTALLATION jobs; the
    # fourth of its jobs is skipped in favour of the other installation's.
    fourth = await store.claim("w-4")
    assert fourth is not None
    assert fourth.job_id == other

    # Only the blocked job remains, and it stays blocked.
    assert await store.claim("w-5") is None


async def test_claim_never_takes_superseded_or_completed_jobs() -> None:
    store = MemoryJobStore(clock=StepClock())
    first = await enqueue(store, make_job(delivery_id="d-1"))
    second = await enqueue(store, make_job(delivery_id="d-2", head_sha=OTHER_SHA))

    claimed = await claim(store)
    assert claimed.job_id == second

    assert await store.complete(second, make_stats(), worker_id="w-1") is True
    superseded = await store.get(first)
    assert superseded is not None
    assert superseded.status is ReviewJobStatus.SUPERSEDED

    assert await store.claim("w-2") is None


# --- holder checks ---------------------------------------------------------


async def test_terminal_transitions_require_the_lease_holder() -> None:
    store = MemoryJobStore(clock=StepClock())
    job_id = await enqueue(store, make_job())
    claimed = await claim(store, worker_id="w-1")

    assert await store.complete(job_id, make_stats(), worker_id="w-2") is False
    assert await store.extend_lease(job_id, worker_id="w-2") is False
    assert await store.fail(job_id, "forge_unavailable", retryable=True, worker_id="w-2") is None
    # The job is untouched: still PROCESSING under w-1.
    stored = await store.get(job_id)
    assert stored is not None
    assert stored.status is ReviewJobStatus.PROCESSING

    assert await store.complete(job_id, make_stats(), worker_id=claimed.worker_id) is True


async def test_terminal_transitions_on_a_missing_job_fail_closed() -> None:
    store = MemoryJobStore()
    assert await store.complete("no-such-job", make_stats(), worker_id="w-1") is False
    assert (
        await store.fail("no-such-job", "forge_unavailable", retryable=True, worker_id="w-1")
        is None
    )
    assert await store.extend_lease("no-such-job", worker_id="w-1") is False


async def test_extend_lease_pushes_the_deadline_out() -> None:
    clock = StepClock()
    store = MemoryJobStore(clock=clock)
    job_id = await enqueue(store, make_job())
    await claim(store, worker_id="w-1")

    clock.advance(120)
    assert await store.extend_lease(job_id, worker_id="w-1") is True
    assert await store.complete(job_id, make_stats(), worker_id="w-1") is True
    # Terminal now: the lease is gone with it.
    clock.advance(1)
    assert await store.extend_lease(job_id, worker_id="w-1") is False


# --- fail: retry, exhaustion, permanent ------------------------------------


async def test_fail_retryable_schedules_the_next_attempt_within_the_backoff_window() -> None:
    clock = StepClock()
    store = MemoryJobStore(clock=clock, rng=random.Random(7))
    job_id = await enqueue(store, make_job())
    claimed = await claim(store)

    failed_at = clock.now()
    status = await store.fail(
        job_id, "forge_unavailable", retryable=True, worker_id=claimed.worker_id
    )

    assert status is ReviewJobStatus.RETRYING
    stored = await store.get(job_id)
    assert stored is not None
    assert stored.status is ReviewJobStatus.RETRYING
    assert stored.retry_count == 1
    assert stored.error_kind == "forge_unavailable"
    assert stored.finished_at is None
    # Backoff takes the pre-increment count (0 here) → base 60 s; jitter
    # ±20% (docs/PIPELINE_SPEC.md §4.3: 1/2/4 min for the three retries).
    assert stored.next_attempt_at is not None
    delay = stored.next_attempt_at - failed_at
    assert timedelta(seconds=60 * 0.8) <= delay <= timedelta(seconds=60 * 1.2)


async def test_fail_retryable_exhausts_after_three_retries() -> None:
    clock = StepClock()
    store = MemoryJobStore(clock=clock, rng=random.Random(7))
    job_id = await enqueue(store, make_job())

    claimed = await claim(store)
    for expected_count in range(1, DEFAULT_MAX_RETRIES + 1):
        status = await store.fail(
            job_id, "forge_unavailable", retryable=True, worker_id=claimed.worker_id
        )
        assert status is ReviewJobStatus.RETRYING
        stored = await store.get(job_id)
        assert stored is not None
        assert stored.retry_count == expected_count
        clock.advance(1000)
        claimed = await claim(store, "w-retry")
        assert claimed.attempt == expected_count

    status = await store.fail(
        job_id, "forge_unavailable", retryable=True, worker_id=claimed.worker_id
    )
    assert status is ReviewJobStatus.FAILED
    stored = await store.get(job_id)
    assert stored is not None
    assert stored.status is ReviewJobStatus.FAILED
    # Capped at max_retries on exhaustion (ck_pr_review_jobs_retry_bounds).
    assert stored.retry_count == DEFAULT_MAX_RETRIES
    assert stored.error_kind == "forge_unavailable"
    assert stored.finished_at is not None
    assert stored.next_attempt_at is None
    assert await store.claim("w-2") is None


async def test_fail_persistent_fails_immediately() -> None:
    clock = StepClock()
    store = MemoryJobStore(clock=clock)
    job_id = await enqueue(store, make_job())
    claimed = await claim(store)

    status = await store.fail(job_id, "pr_not_found", retryable=False, worker_id=claimed.worker_id)

    assert status is ReviewJobStatus.FAILED
    stored = await store.get(job_id)
    assert stored is not None
    assert stored.status is ReviewJobStatus.FAILED
    assert stored.retry_count == 0
    assert stored.error_kind == "pr_not_found"
    assert stored.finished_at is not None


async def test_fail_rejects_empty_error_kind() -> None:
    store = MemoryJobStore()
    await enqueue(store, make_job())
    claimed = await claim(store)
    with pytest.raises(ValueError, match="error_kind"):
        await store.fail(claimed.job_id, "  ", retryable=True, worker_id="w-1")


async def test_error_kind_never_leaks_exception_content() -> None:
    store = MemoryJobStore()
    await enqueue(store, make_job())
    claimed = await claim(store)
    err = ForgeUnavailableError("github", "connection reset by peer")
    status = await store.fail(claimed.job_id, type(err).__name__, retryable=False, worker_id="w-1")
    assert status is ReviewJobStatus.FAILED
    stored = await store.get(claimed.job_id)
    assert stored is not None
    assert stored.error_kind == "ForgeUnavailableError"
    assert str(err) not in stored.error_kind


async def test_get_missing_job_returns_none() -> None:
    store = MemoryJobStore()
    assert await store.get("no-such-job") is None


# --- injected clock --------------------------------------------------------


async def test_injected_clock_determines_created_at_and_finished_at() -> None:
    clock = StepClock()
    store = MemoryJobStore(clock=clock)

    first_at = clock.now()
    first = await store.enqueue(make_job(delivery_id="d-1"))
    assert first is not None
    stored = await store.get(first)
    assert stored is not None
    assert stored.created_at == first_at

    clock.advance(30)
    superseded_at = clock.now()
    second = await store.enqueue(make_job(delivery_id="d-2"))
    assert second is not None
    superseded = await store.get(first)
    assert superseded is not None
    assert superseded.status is ReviewJobStatus.SUPERSEDED
    assert superseded.finished_at == superseded_at
    successor = await store.get(second)
    assert successor is not None
    assert successor.created_at == superseded_at

    clock.advance(5)
    claimed = await claim(store)
    clock.advance(10)
    completed_at = clock.now()
    assert await store.complete(second, make_stats(), worker_id=claimed.worker_id)
    completed = await store.get(second)
    assert completed is not None
    assert completed.finished_at == completed_at
