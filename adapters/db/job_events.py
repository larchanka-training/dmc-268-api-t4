"""Job progress in PostgreSQL: the worker writes `pr_review_steps`, the API reads them.

`PostgresStepRecorder` inserts a RUNNING row when a step starts and fills in
`finished_at` and the outcome when it ends. Both statements join `pr_review_jobs` on
`status = 'PROCESSING' AND locked_by = :worker`, so a worker that lost its lease
writes nothing. Times come from `clock_timestamp()`, so they follow the real order of
the steps.

`PostgresJobEventsSource` rebuilds the event history from the job row and its step
rows (`domain.jobs.events_from_steps`) and re-reads it every `POLL_SECONDS` while a
stream waits.
"""

import asyncio
import json
import time
import uuid
from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from domain.jobs import (
    JobEvent,
    JobSnapshot,
    JobStep,
    StepRecord,
    StepStatus,
    events_from_steps,
    latest_steps,
)
from domain.ports import ReviewJobStatus

#: How often a waiting stream re-reads the job's rows.
POLL_SECONDS = 1.0


class PostgresStepRecorder:
    """Implements `domain.ports.StepRecorder` over `pr_review_steps`."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def start(self, job_id: str, attempt: int, step: JobStep, *, worker_id: str) -> None:
        row_id = _as_uuid(job_id)
        if row_id is None:
            return
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO pr_review_steps"
                    " (id, job_id, attempt, seq, step, status, started_at)"
                    " SELECT :id, j.id, :attempt,"
                    " COALESCE((SELECT max(s.seq) FROM pr_review_steps s"
                    " WHERE s.job_id = j.id AND s.attempt = :attempt), 0) + 1,"
                    " :step, 'RUNNING', clock_timestamp()"
                    " FROM pr_review_jobs j"
                    " WHERE j.id = :job_id AND j.status = 'PROCESSING' AND j.locked_by = :worker"
                ),
                {
                    "id": uuid.uuid4(),
                    "job_id": row_id,
                    "attempt": attempt,
                    "step": str(step),
                    "worker": worker_id,
                },
            )

    async def finish(
        self,
        job_id: str,
        attempt: int,
        step: JobStep,
        status: StepStatus,
        *,
        worker_id: str,
        error_kind: str | None = None,
        metrics: Mapping[str, int | str] | None = None,
    ) -> None:
        row_id = _as_uuid(job_id)
        if row_id is None:
            return
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE pr_review_steps s"
                    " SET status = :status, finished_at = clock_timestamp(),"
                    " error_kind = :error_kind,"
                    " metrics = COALESCE(CAST(:metrics AS jsonb), metrics)"
                    " FROM pr_review_jobs j"
                    " WHERE s.job_id = j.id AND j.id = :job_id"
                    " AND j.status = 'PROCESSING' AND j.locked_by = :worker"
                    " AND s.attempt = :attempt AND s.step = :step AND s.finished_at IS NULL"
                ),
                {
                    "status": str(status),
                    "error_kind": error_kind,
                    "metrics": json.dumps(dict(metrics)) if metrics else None,
                    "job_id": row_id,
                    "worker": worker_id,
                    "attempt": attempt,
                    "step": str(step),
                },
            )


class PostgresJobEventsSource:
    """Implements `domain.ports.JobEventsSource` over `pr_review_jobs` and `pr_review_steps`."""

    def __init__(self, engine: AsyncEngine, *, poll_seconds: float = POLL_SECONDS) -> None:
        self._engine = engine
        self._poll_seconds = poll_seconds

    async def snapshot(self, job_id: str) -> JobSnapshot | None:
        read = await self._read(job_id)
        if read is None:
            return None
        installation_id, status, events = read
        return JobSnapshot(
            job_id=job_id,
            installation_id=installation_id,
            status=status,
            last_seq=len(events),
            steps=latest_steps(events),
        )

    async def next_events(
        self, job_id: str, after_seq: int, timeout_seconds: float
    ) -> tuple[JobEvent, ...]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            read = await self._read(job_id)
            if read is not None:
                pending = read[2][after_seq:]
                if pending:
                    return pending
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ()
            await asyncio.sleep(min(self._poll_seconds, remaining))

    async def _read(self, job_id: str) -> tuple[int, ReviewJobStatus, tuple[JobEvent, ...]] | None:
        row_id = _as_uuid(job_id)
        if row_id is None:
            return None
        async with self._engine.connect() as conn:
            job = (
                await conn.execute(
                    text(
                        "SELECT installation_id, status, retry_count,"
                        " COALESCE(finished_at, started_at, created_at) AS status_at"
                        " FROM pr_review_jobs WHERE id = :id"
                    ),
                    {"id": row_id},
                )
            ).first()
            if job is None:
                return None
            rows = (
                await conn.execute(
                    text(
                        "SELECT step, attempt, status, started_at, finished_at, error_kind, metrics"
                        " FROM pr_review_steps WHERE job_id = :id ORDER BY attempt, seq"
                    ),
                    {"id": row_id},
                )
            ).all()
        status = ReviewJobStatus(job.status)
        steps = [
            StepRecord(
                step=JobStep(row.step),
                attempt=int(row.attempt),
                status=StepStatus(row.status),
                started_at=row.started_at,
                finished_at=row.finished_at,
                error_kind=row.error_kind,
                metrics=dict(row.metrics) if row.metrics else None,
            )
            for row in rows
        ]
        events = events_from_steps(job_id, status, int(job.retry_count), job.status_at, steps)
        return int(job.installation_id), status, events


def _as_uuid(job_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(job_id)
    except ValueError:
        return None
