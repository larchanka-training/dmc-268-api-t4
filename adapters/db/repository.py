"""PostgreSQL implementation of the JobRepository port.

Everything here is raw ``text()`` SQL, never the ORM: enqueue and claim are
exactly the single-transaction / single-statement designs of
docs/WORKFLOW_DESIGN.md §2 Steps 3–4 and docs/db_models_and_migrations.md §5,
which is why the schema's timestamp columns carry server defaults
(docs/db_models_and_migrations.md §1.1, created_at comment). Time inside those
statements is the database's ``now()``; the injected clock and rng are used
only where the port's semantics are computed in Python (``fail``'s backoff) —
the same code path as the in-memory store.
"""

import json
import random
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncEngine

from adapters.clock import SystemClock
from domain.jobs import LEASE_SECONDS, MAX_IN_FLIGHT_PER_INSTALLATION, backoff_delay
from domain.ports import (
    ClaimedJob,
    Clock,
    JobStats,
    NewReviewJob,
    ReviewJob,
    ReviewJobStatus,
)

#: docs/WORKFLOW_DESIGN.md §2 Step 3 — the channel the worker LISTENs on.
NOTIFY_CHANNEL = "review_jobs"

_TERMINAL_STATUSES = frozenset(
    {
        ReviewJobStatus.COMPLETED,
        ReviewJobStatus.FAILED,
        ReviewJobStatus.SUPERSEDED,
        ReviewJobStatus.SKIPPED,
    }
)


class PostgresJobStore:
    """Implements domain.ports.JobRepository over the ``pr_review_jobs`` table."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        clock: Clock | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock or SystemClock()
        self._rng = rng or random.Random()

    async def enqueue(self, job: NewReviewJob) -> str | None:
        """One transaction (WORKFLOW_DESIGN.md §2 Step 3): dedup, supersede,
        insert, notify.

        The dedup check runs BEFORE the supersede — the order of
        MemoryJobStore (see its docstring), deliberately not the order of the
        docs' SQL: a redelivered webhook must not supersede the active jobs of
        its own pull request, so a duplicate delivery returns None having
        changed nothing. The INSERT keeps ``ON CONFLICT (provider, delivery_id)
        DO NOTHING`` as the race backstop for two concurrent transactions with
        the same delivery that both pass the SELECT: one insert wins, the
        loser sees rowcount 0 and returns None with the supersede it did apply
        still committed — a duplicate that supersedes nothing must not undo
        step 1 either. A NULL delivery_id never conflicts (documented
        PostgreSQL semantics — WORKFLOW_DESIGN.md §4 layer 1).
        """
        row_id = uuid.uuid4()
        async with self._engine.begin() as conn:
            if job.delivery_id is not None:
                duplicate = await conn.execute(
                    text(
                        "SELECT 1 FROM pr_review_jobs"
                        " WHERE provider = :provider AND delivery_id = :delivery_id"
                        " LIMIT 1"
                    ),
                    {"provider": job.provider, "delivery_id": job.delivery_id},
                )
                if duplicate.first() is not None:
                    return None
            await conn.execute(
                text(
                    "UPDATE pr_review_jobs"
                    " SET status = 'SUPERSEDED', finished_at = now()"
                    " WHERE installation_id = :installation_id"
                    " AND repo_id = :repo_id AND pr_number = :pr_number"
                    " AND status IN ('QUEUED','RETRYING','PROCESSING')"
                ),
                {
                    "installation_id": job.installation_id,
                    "repo_id": job.repo_id,
                    "pr_number": job.pr_number,
                },
            )
            inserted = await conn.execute(
                text(
                    "INSERT INTO pr_review_jobs"
                    " (id, delivery_id, provider, installation_id, repo_id,"
                    " repo_full_name, pr_number, head_sha, base_sha, event_action,"
                    " pr_title, author_login, head_ref, base_ref, status,"
                    " next_attempt_at)"
                    " VALUES (:id, :delivery_id, :provider, :installation_id, :repo_id,"
                    " :repo_full_name, :pr_number, :head_sha, :base_sha, :event_action,"
                    " :pr_title, :author_login, :head_ref, :base_ref, 'QUEUED', now())"
                    " ON CONFLICT (provider, delivery_id) DO NOTHING"
                ),
                {
                    "id": row_id,
                    "delivery_id": job.delivery_id,
                    "provider": job.provider,
                    "installation_id": job.installation_id,
                    "repo_id": job.repo_id,
                    "repo_full_name": job.repo_full_name,
                    "pr_number": job.pr_number,
                    "head_sha": job.head_sha,
                    "base_sha": job.base_sha,
                    "event_action": job.event_action,
                    "pr_title": job.pr_title,
                    "author_login": job.author_login,
                    "head_ref": job.head_ref,
                    "base_ref": job.base_ref,
                },
            )
            if inserted.rowcount == 0:
                return None
            await conn.execute(text("SELECT pg_notify(:channel, '')"), {"channel": NOTIFY_CHANNEL})
        return row_id.hex

    async def claim(self, worker_id: str) -> ClaimedJob | None:
        """The single claim statement of docs/WORKFLOW_DESIGN.md §2 Step 4.

        The fairness subquery is keyed on installation_id rather than
        account_id — the documented MVP deviation until tenancy lands
        (domain.jobs.MAX_IN_FLIGHT_PER_INSTALLATION). The transaction commits
        here; the review runs outside it, protected by the lease.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    "UPDATE pr_review_jobs"
                    " SET status = 'PROCESSING', locked_by = :worker,"
                    " locked_until = now() + make_interval(secs => :lease),"
                    " started_at = now()"
                    " WHERE id = ("
                    " SELECT j.id FROM pr_review_jobs j"
                    " WHERE j.status IN ('QUEUED','RETRYING')"
                    " AND j.next_attempt_at <= now()"
                    " AND (SELECT count(*) FROM pr_review_jobs p"
                    " WHERE p.status = 'PROCESSING'"
                    " AND p.installation_id = j.installation_id) < :max_in_flight"
                    " ORDER BY j.next_attempt_at, j.created_at"
                    " FOR UPDATE SKIP LOCKED LIMIT 1)"
                    " RETURNING id, provider, delivery_id, installation_id, repo_id,"
                    " repo_full_name, pr_number, head_sha, base_sha, event_action,"
                    " pr_title, author_login, head_ref, base_ref, retry_count"
                ),
                {
                    "worker": worker_id,
                    "lease": float(LEASE_SECONDS),
                    "max_in_flight": MAX_IN_FLIGHT_PER_INSTALLATION,
                },
            )
            row = result.first()
        if row is None:
            return None
        return ClaimedJob(
            job_id=str(row.id.hex),
            payload=_payload_from_row(row),
            attempt=int(row.retry_count),
            worker_id=worker_id,
        )

    async def complete(self, job_id: str, stats: JobStats, *, worker_id: str) -> bool:
        row_id = _as_uuid(job_id)
        if row_id is None:
            return False
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    "UPDATE pr_review_jobs"
                    " SET status = 'COMPLETED', stats = CAST(:stats AS jsonb),"
                    " finished_at = now()"
                    " WHERE id = :id AND status = 'PROCESSING' AND locked_by = :worker"
                ),
                {"stats": _stats_to_json(stats), "id": row_id, "worker": worker_id},
            )
            return result.rowcount == 1

    async def fail(
        self, job_id: str, error_kind: str, *, retryable: bool, worker_id: str
    ) -> ReviewJobStatus | None:
        if not error_kind.strip():
            raise ValueError("error_kind must not be empty")
        row_id = _as_uuid(job_id)
        if row_id is None:
            return None
        async with self._engine.begin() as conn:
            held = await conn.execute(
                text(
                    "SELECT status, locked_by, retry_count, max_retries,"
                    " next_attempt_at FROM pr_review_jobs WHERE id = :id FOR UPDATE"
                ),
                {"id": row_id},
            )
            row = held.first()
            # Holder check in Python: FOR UPDATE row lock makes the
            # read-decide-write below atomic under concurrency.
            if (
                row is None
                or row.status != ReviewJobStatus.PROCESSING
                or row.locked_by != worker_id
            ):
                return None
            old_retry_count = int(row.retry_count)
            next_retry = old_retry_count + 1
            if retryable and next_retry <= int(row.max_retries):
                new_status = ReviewJobStatus.RETRYING
                new_retry_count = next_retry
                # Same code path as the memory store: backoff on the
                # pre-increment count (docs/PIPELINE_SPEC.md §4.3) with the
                # injected clock and rng.
                next_attempt_at: Any = self._clock.now() + backoff_delay(old_retry_count, self._rng)
            else:
                new_status = ReviewJobStatus.FAILED
                # Capped at max_retries on exhaustion
                # (ck_pr_review_jobs_retry_bounds). The column is NOT NULL, so
                # a FAILED row keeps its last scheduled attempt.
                new_retry_count = int(row.max_retries) if retryable else old_retry_count
                next_attempt_at = row.next_attempt_at
            await conn.execute(
                text(
                    "UPDATE pr_review_jobs"
                    " SET status = :status, retry_count = :retry_count,"
                    " next_attempt_at = :next_attempt_at, error_kind = :error_kind,"
                    " finished_at = CASE WHEN :failed THEN now() ELSE finished_at END,"
                    " locked_by = NULL, locked_until = NULL"
                    " WHERE id = :id"
                ),
                {
                    "status": new_status,
                    "retry_count": new_retry_count,
                    "next_attempt_at": next_attempt_at,
                    "error_kind": error_kind,
                    "failed": new_status is ReviewJobStatus.FAILED,
                    "id": row_id,
                },
            )
        return new_status

    async def extend_lease(self, job_id: str, *, worker_id: str) -> bool:
        row_id = _as_uuid(job_id)
        if row_id is None:
            return False
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    "UPDATE pr_review_jobs"
                    " SET locked_until = now() + make_interval(secs => :lease)"
                    " WHERE id = :id AND status = 'PROCESSING' AND locked_by = :worker"
                ),
                {"lease": float(LEASE_SECONDS), "id": row_id, "worker": worker_id},
            )
            return result.rowcount == 1

    async def get(self, job_id: str) -> ReviewJob | None:
        row_id = _as_uuid(job_id)
        if row_id is None:
            return None
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT id, delivery_id, provider, installation_id, repo_id,"
                    " repo_full_name, pr_number, head_sha, base_sha, event_action,"
                    " pr_title, author_login, head_ref, base_ref, status, stats,"
                    " error_kind, created_at, finished_at, retry_count, next_attempt_at"
                    " FROM pr_review_jobs WHERE id = :id"
                ),
                {"id": row_id},
            )
            row = result.first()
        if row is None:
            return None
        status = ReviewJobStatus(row.status)
        return ReviewJob(
            job_id=str(row.id.hex),
            created_at=row.created_at,
            status=status,
            payload=_payload_from_row(row),
            stats=_stats_from_json(row.stats),
            error_kind=row.error_kind,
            finished_at=row.finished_at,
            retry_count=int(row.retry_count),
            # The column is NOT NULL, so a terminal row keeps its last
            # scheduled attempt on disk; through the port there is no next
            # attempt, exactly like the memory store.
            next_attempt_at=None if status in _TERMINAL_STATUSES else row.next_attempt_at,
        )


def _as_uuid(job_id: str) -> uuid.UUID | None:
    """Job ids are uuid4 hex strings; anything else is simply no job."""
    try:
        return uuid.UUID(job_id)
    except ValueError:
        return None


def _payload_from_row(row: Row[Any]) -> NewReviewJob:
    return NewReviewJob(
        provider=row.provider,
        delivery_id=row.delivery_id,
        installation_id=row.installation_id,
        repo_id=row.repo_id,
        repo_full_name=row.repo_full_name,
        pr_number=row.pr_number,
        head_sha=row.head_sha,
        base_sha=row.base_sha,
        event_action=row.event_action,
        pr_title=row.pr_title,
        author_login=row.author_login,
        head_ref=row.head_ref,
        base_ref=row.base_ref,
    )


def _stats_to_json(stats: JobStats) -> str:
    return json.dumps(
        {
            "files_total": stats.files_total,
            "files_reviewable": stats.files_reviewable,
            "hunks_total": stats.hunks_total,
            "chunks_total": stats.chunks_total,
            "skipped_counts": [[reason, count] for reason, count in stats.skipped_counts],
        }
    )


def _stats_from_json(raw: Any) -> JobStats | None:
    if raw is None:
        return None
    # asyncpg hands raw-SELECT jsonb back as text; accept either shape.
    value = json.loads(raw) if isinstance(raw, str) else raw
    return JobStats(
        files_total=int(value["files_total"]),
        files_reviewable=int(value["files_reviewable"]),
        hunks_total=int(value["hunks_total"]),
        chunks_total=int(value["chunks_total"]),
        skipped_counts=tuple(
            (str(reason), int(count)) for reason, count in value["skipped_counts"]
        ),
    )
