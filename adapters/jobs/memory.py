"""In-memory review job store.

A stand-in for the PostgreSQL queue: `adapters/db/jobs.py` replaces it behind
the same `domain.ports.JobRepository` port (decision 1 in docs/tasks/plan.md,
2026-10-07; DDL in docs/db_models_and_migrations.md). Enqueue mirrors the
single transaction of docs/WORKFLOW_DESIGN.md §2 Step 3 — dedup, supersede,
insert — under one lock here. The dedup check deliberately runs before the
supersede pass, a reorder of that SQL (which supersedes first and dedups on
insert): a duplicate delivery must not supersede its own active job.

Claim mirrors §2 Step 4: the oldest eligible QUEUED/RETRYING row whose
installation still has in-flight slots becomes PROCESSING with a lease held
by the claiming worker. Retry timing follows PIPELINE_SPEC §4 via
`domain.jobs.backoff_delay` with the injected rng.
"""

import asyncio
import random
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from adapters.clock import SystemClock
from domain.jobs import (
    DEFAULT_MAX_RETRIES,
    LEASE_SECONDS,
    MAX_IN_FLIGHT_PER_INSTALLATION,
    JobPage,
    JobQuery,
    StepRecord,
    backoff_delay,
)
from domain.ports import (
    QUEUE_STATUSES,
    ClaimedJob,
    Clock,
    JobStats,
    NewReviewJob,
    PeriodicTaskRun,
    QueueSnapshot,
    ReviewJob,
    ReviewJobStatus,
)

#: docs/WORKFLOW_DESIGN.md §2 Step 3: a new push supersedes QUEUED, RETRYING
#: and PROCESSING jobs for the same pull request.
_ACTIVE_STATUSES = frozenset(
    {ReviewJobStatus.QUEUED, ReviewJobStatus.RETRYING, ReviewJobStatus.PROCESSING}
)
_CLAIMABLE_STATUSES = frozenset({ReviewJobStatus.QUEUED, ReviewJobStatus.RETRYING})


@dataclass(frozen=True, slots=True)
class _Record:
    """Internal row: the public ReviewJob plus the lease columns."""

    job: ReviewJob
    locked_by: str | None = None
    locked_until: datetime | None = None


class MemoryJobStore:
    """Implements domain.ports.JobRepository over a dict keyed by job id."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._rng = rng or random.Random()
        self._records: dict[str, _Record] = {}
        self._periodic: dict[str, PeriodicTaskRun] = {}
        self._lock = asyncio.Lock()

    async def enqueue(self, job: NewReviewJob) -> str | None:
        """Record a job, dedup its delivery, supersede active jobs for the PR.

        Returns the new job id, or None when the same (provider, delivery_id)
        was already recorded in any status; a None delivery_id never dedups.
        """
        async with self._lock:
            now = self._clock.now()
            if job.delivery_id is not None:
                for existing in self._records.values():
                    payload = existing.job.payload
                    if payload.provider == job.provider and payload.delivery_id == job.delivery_id:
                        return None
            for existing in self._records.values():
                payload = existing.job.payload
                if (
                    payload.installation_id == job.installation_id
                    and payload.repo_id == job.repo_id
                    and payload.pr_number == job.pr_number
                    and existing.job.status in _ACTIVE_STATUSES
                ):
                    self._records[existing.job.job_id] = _Record(
                        job=replace(
                            existing.job,
                            status=ReviewJobStatus.SUPERSEDED,
                            finished_at=now,
                        )
                    )
            job_id = uuid.uuid4().hex
            self._records[job_id] = _Record(
                job=ReviewJob(
                    job_id=job_id,
                    created_at=now,
                    status=ReviewJobStatus.QUEUED,
                    payload=job,
                    # The schema's server_default now() (db_models §5): a
                    # fresh row is immediately claimable, and claim never
                    # sees a NULL next_attempt_at.
                    next_attempt_at=now,
                )
            )
            return job_id

    async def claim(self, worker_id: str) -> ClaimedJob | None:
        async with self._lock:
            now = self._clock.now()
            in_flight: dict[int, int] = {}
            for record in self._records.values():
                if record.job.status is ReviewJobStatus.PROCESSING:
                    installation = record.job.payload.installation_id
                    in_flight[installation] = in_flight.get(installation, 0) + 1
            eligible = [
                record
                for record in self._records.values()
                if record.job.status in _CLAIMABLE_STATUSES
                and record.job.next_attempt_at is not None
                and record.job.next_attempt_at <= now
                and in_flight.get(record.job.payload.installation_id, 0)
                < MAX_IN_FLIGHT_PER_INSTALLATION
            ]
            if not eligible:
                return None
            # Ordered like the claim SQL (docs/WORKFLOW_DESIGN.md §2
            # Step 4): next_attempt_at (set at enqueue and after each
            # retry), then created_at. The column is NOT NULL in the
            # schema, so no NULLS LAST special case is needed.
            oldest = min(
                eligible,
                key=lambda record: (
                    record.job.next_attempt_at,
                    record.job.created_at,
                ),
            )
            self._records[oldest.job.job_id] = _Record(
                job=replace(oldest.job, status=ReviewJobStatus.PROCESSING, started_at=now),
                locked_by=worker_id,
                locked_until=now + timedelta(seconds=LEASE_SECONDS),
            )
            return ClaimedJob(
                job_id=oldest.job.job_id,
                payload=oldest.job.payload,
                attempt=oldest.job.retry_count,
                worker_id=worker_id,
                enqueued_at=oldest.job.created_at,
                claimable_at=oldest.job.next_attempt_at,
                claimed_at=now,
            )

    async def complete(
        self, job_id: str, stats: JobStats, *, worker_id: str, summary: str | None = None
    ) -> bool:
        async with self._lock:
            record = self._records.get(job_id)
            if record is None or not _held_by(record, worker_id):
                return False
            self._records[job_id] = _Record(
                job=replace(
                    record.job,
                    status=ReviewJobStatus.COMPLETED,
                    stats=stats,
                    summary=summary,
                    finished_at=self._clock.now(),
                    next_attempt_at=None,
                )
            )
            return True

    async def fail(
        self, job_id: str, error_kind: str, *, retryable: bool, worker_id: str
    ) -> ReviewJobStatus | None:
        if not error_kind.strip():
            raise ValueError("error_kind must not be empty")
        async with self._lock:
            record = self._records.get(job_id)
            if record is None or not _held_by(record, worker_id):
                return None
            now = self._clock.now()
            old_retry_count = record.job.retry_count
            retry_count = old_retry_count + 1
            if retryable and retry_count <= DEFAULT_MAX_RETRIES:
                self._records[job_id] = _Record(
                    job=replace(
                        record.job,
                        status=ReviewJobStatus.RETRYING,
                        retry_count=retry_count,
                        error_kind=error_kind,
                        # Backoff on the pre-increment count — 1/2/4 min
                        # for the three retries (docs/PIPELINE_SPEC.md
                        # §4.3; the reap SQL in db_models §5).
                        next_attempt_at=now + backoff_delay(old_retry_count, self._rng),
                    )
                )
                return ReviewJobStatus.RETRYING
            self._records[job_id] = _Record(
                job=replace(
                    record.job,
                    status=ReviewJobStatus.FAILED,
                    # Capped at max_retries on exhaustion:
                    # ck_pr_review_jobs_retry_bounds.
                    retry_count=DEFAULT_MAX_RETRIES if retryable else old_retry_count,
                    error_kind=error_kind,
                    finished_at=now,
                    next_attempt_at=None,
                )
            )
            return ReviewJobStatus.FAILED

    async def extend_lease(self, job_id: str, *, worker_id: str) -> bool:
        async with self._lock:
            record = self._records.get(job_id)
            if record is None or not _held_by(record, worker_id):
                return False
            self._records[job_id] = replace(
                record,
                locked_until=self._clock.now() + timedelta(seconds=LEASE_SECONDS),
            )
            return True

    async def record_periodic_run(self, task: str, interval_seconds: float) -> None:
        """The Postgres store's upsert, for tests: the memory mode runs no reaper."""
        async with self._lock:
            self._periodic[task] = PeriodicTaskRun(task, self._clock.now(), interval_seconds)

    async def periodic_runs(self) -> tuple[PeriodicTaskRun, ...]:
        async with self._lock:
            return tuple(self._periodic[task] for task in sorted(self._periodic))

    async def queue_snapshot(self) -> QueueSnapshot:
        """Same counts and age as the Postgres store's: the DB sampler's twin."""
        async with self._lock:
            now = self._clock.now()
            jobs = [record.job for record in self._records.values()]
        claimable = [
            job.next_attempt_at
            for job in jobs
            if job.status in _CLAIMABLE_STATUSES
            and job.next_attempt_at is not None
            and job.next_attempt_at <= now
        ]
        return QueueSnapshot(
            jobs=tuple(
                (status, sum(job.status is status for job in jobs)) for status in QUEUE_STATUSES
            ),
            oldest_claimable_age_seconds=(
                (now - min(claimable)).total_seconds() if claimable else 0.0
            ),
        )

    async def get(self, job_id: str) -> ReviewJob | None:
        async with self._lock:
            record = self._records.get(job_id)
            return record.job if record is not None else None

    async def list_page(self, query: JobQuery) -> JobPage:
        async with self._lock:
            matching = sorted(
                (record.job for record in self._records.values() if _matches(record.job, query)),
                key=lambda job: (job.created_at, job.job_id),
                reverse=True,
            )
        page = matching[: query.limit]
        more = len(matching) > query.limit
        return JobPage(
            items=tuple(page),
            next_after=(page[-1].created_at, page[-1].job_id) if more else None,
        )

    async def steps(self, job_id: str) -> tuple[StepRecord, ...]:
        # The in-memory dev mode records no steps.
        return ()


def _matches(job: ReviewJob, query: JobQuery) -> bool:
    payload = job.payload
    if payload.installation_id not in query.installation_ids:
        return False
    if query.statuses and job.status not in query.statuses:
        return False
    if query.repository is not None and payload.repo_full_name != query.repository:
        return False
    if query.author is not None and payload.author_login != query.author:
        return False
    if query.created_from is not None and job.created_at < query.created_from:
        return False
    if query.created_to is not None and job.created_at > query.created_to:
        return False
    if query.text is not None:
        number = query.text.strip().lstrip("#")
        in_title = query.text.lower() in payload.pr_title.lower()
        if not in_title and not (number.isdigit() and int(number) == payload.pr_number):
            return False
    return query.after is None or (job.created_at, job.job_id) < query.after


def _held_by(record: _Record, worker_id: str) -> bool:
    """The lease check shared by complete/fail/extend_lease."""
    return record.job.status is ReviewJobStatus.PROCESSING and record.locked_by == worker_id
