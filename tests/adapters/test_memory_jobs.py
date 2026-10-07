"""MemoryJobStore: dedup, supersession, status transitions, injected clock.

Mirrors the enqueue transaction of docs/WORKFLOW_DESIGN.md §2 Step 3 — one
lock here stands in for the single transaction there.
"""

from datetime import UTC, datetime, timedelta

import pytest

from adapters.jobs.memory import MemoryJobStore
from domain.errors import ForgeUnavailableError
from domain.ports import JobStats, NewReviewJob, ReviewJob, ReviewJobStatus

HEAD_SHA = "a1b2c3d4e5" * 4
BASE_SHA = "f6e5d4c3b2" * 4
OTHER_SHA = "0123456789" * 4


class StepClock:
    """A domain.ports.Clock fake; advance it instead of sleeping."""

    def __init__(self) -> None:
        self._now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: int) -> None:
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


async def drive_to_processing(store: MemoryJobStore, job: NewReviewJob) -> str:
    """Enqueue and claim a job, returning its id."""
    job_id = await store.enqueue(job)
    assert job_id is not None
    assert await store.mark_processing(job_id)
    return job_id


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
    )
    assert stored is not None
    assert stored.created_at.tzinfo is UTC
    assert stored.stats is None
    assert stored.error_kind is None
    assert stored.finished_at is None


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
    job_id = await drive_to_processing(store, make_job(delivery_id="d-1"))
    assert await store.mark_completed(job_id, make_stats())
    assert await store.enqueue(make_job(delivery_id="d-2")) is not None

    stored = await store.get(job_id)
    assert stored is not None
    assert stored.status is ReviewJobStatus.COMPLETED


# --- status machine --------------------------------------------------------


async def test_mark_processing_from_queued() -> None:
    store = MemoryJobStore()
    job_id = await store.enqueue(make_job())
    assert job_id is not None
    assert await store.mark_processing(job_id) is True
    stored = await store.get(job_id)
    assert stored is not None
    assert stored.status is ReviewJobStatus.PROCESSING


async def test_mark_processing_rejects_missing_job() -> None:
    store = MemoryJobStore()
    assert await store.mark_processing("no-such-job") is False


async def test_mark_processing_rejects_superseded_job() -> None:
    store = MemoryJobStore()
    first = await store.enqueue(make_job(delivery_id="d-1"))
    assert first is not None
    assert await store.enqueue(make_job(delivery_id="d-2")) is not None
    assert await store.mark_processing(first) is False


async def test_mark_processing_rejects_already_processing_job() -> None:
    store = MemoryJobStore()
    job_id = await drive_to_processing(store, make_job())
    assert await store.mark_processing(job_id) is False


async def test_mark_completed_only_from_processing() -> None:
    store = MemoryJobStore()
    job_id = await store.enqueue(make_job())
    assert job_id is not None
    assert await store.mark_completed(job_id, make_stats()) is False

    assert await store.mark_processing(job_id)
    stats = make_stats()
    assert await store.mark_completed(job_id, stats) is True

    stored = await store.get(job_id)
    assert stored is not None
    assert stored.status is ReviewJobStatus.COMPLETED
    assert stored.stats == stats
    assert stored.error_kind is None
    assert stored.finished_at is not None

    assert await store.mark_completed(job_id, stats) is False


async def test_mark_failed_only_from_processing() -> None:
    store = MemoryJobStore()
    job_id = await store.enqueue(make_job())
    assert job_id is not None
    assert await store.mark_failed(job_id, "ForgeUnavailableError") is False

    assert await store.mark_processing(job_id)
    assert await store.mark_failed(job_id, "ForgeUnavailableError") is True

    stored = await store.get(job_id)
    assert stored is not None
    assert stored.status is ReviewJobStatus.FAILED
    assert stored.error_kind == "ForgeUnavailableError"
    assert stored.stats is None
    assert stored.finished_at is not None

    assert await store.mark_failed(job_id, "ForgeUnavailableError") is False


async def test_mark_failed_rejects_empty_error_kind() -> None:
    store = MemoryJobStore()
    job_id = await drive_to_processing(store, make_job())
    with pytest.raises(ValueError, match="error_kind"):
        await store.mark_failed(job_id, "  ")


async def test_error_kind_never_leaks_exception_content() -> None:
    store = MemoryJobStore()
    job_id = await drive_to_processing(store, make_job())
    err = ForgeUnavailableError("github", "connection reset by peer")
    assert await store.mark_failed(job_id, type(err).__name__) is True
    stored = await store.get(job_id)
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
    assert await store.mark_processing(second)
    clock.advance(10)
    completed_at = clock.now()
    assert await store.mark_completed(second, make_stats())
    completed = await store.get(second)
    assert completed is not None
    assert completed.finished_at == completed_at
