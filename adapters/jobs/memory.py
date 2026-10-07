"""In-memory review job store.

A stand-in for the PostgreSQL queue: `adapters/db/jobs.py` replaces it behind
the same `domain.ports.JobRepository` port (decision 1 in tasks/plan.md,
2026-10-07; DDL in docs/db_models_and_migrations.md). Enqueue mirrors the
single transaction of docs/WORKFLOW_DESIGN.md §2 Step 3 — dedup, supersede,
insert — under one lock here. The dedup check deliberately runs before the
supersede pass, a reorder of that SQL (which supersedes first and dedups on
insert): a duplicate delivery must not supersede its own active job.
"""

import asyncio
import uuid
from dataclasses import replace

from adapters.clock import SystemClock
from domain.ports import Clock, JobStats, NewReviewJob, ReviewJob, ReviewJobStatus

_ACTIVE_STATUSES = frozenset({ReviewJobStatus.QUEUED, ReviewJobStatus.PROCESSING})


class MemoryJobStore:
    """Implements domain.ports.JobRepository over a dict keyed by job id."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._jobs: dict[str, ReviewJob] = {}
        self._lock = asyncio.Lock()

    async def enqueue(self, job: NewReviewJob) -> str | None:
        """Record a job, dedup its delivery, supersede active jobs for the PR.

        Returns the new job id, or None when the same (provider, delivery_id)
        was already recorded in any status; a None delivery_id never dedups.
        """
        async with self._lock:
            now = self._clock.now()
            if job.delivery_id is not None:
                for existing in self._jobs.values():
                    payload = existing.payload
                    if payload.provider == job.provider and payload.delivery_id == job.delivery_id:
                        return None
            for existing in self._jobs.values():
                payload = existing.payload
                if (
                    payload.installation_id == job.installation_id
                    and payload.repo_id == job.repo_id
                    and payload.pr_number == job.pr_number
                    and existing.status in _ACTIVE_STATUSES
                ):
                    self._jobs[existing.job_id] = replace(
                        existing, status=ReviewJobStatus.SUPERSEDED, finished_at=now
                    )
            job_id = uuid.uuid4().hex
            self._jobs[job_id] = ReviewJob(
                job_id=job_id,
                created_at=now,
                status=ReviewJobStatus.QUEUED,
                payload=job,
            )
            return job_id

    async def mark_processing(self, job_id: str) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status is not ReviewJobStatus.QUEUED:
                return False
            self._jobs[job_id] = replace(job, status=ReviewJobStatus.PROCESSING)
            return True

    async def mark_completed(self, job_id: str, stats: JobStats) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status is not ReviewJobStatus.PROCESSING:
                return False
            self._jobs[job_id] = replace(
                job,
                status=ReviewJobStatus.COMPLETED,
                stats=stats,
                finished_at=self._clock.now(),
            )
            return True

    async def mark_failed(self, job_id: str, error_kind: str) -> bool:
        if not error_kind.strip():
            raise ValueError("error_kind must not be empty")
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status is not ReviewJobStatus.PROCESSING:
                return False
            self._jobs[job_id] = replace(
                job,
                status=ReviewJobStatus.FAILED,
                error_kind=error_kind,
                finished_at=self._clock.now(),
            )
            return True

    async def get(self, job_id: str) -> ReviewJob | None:
        async with self._lock:
            return self._jobs.get(job_id)
