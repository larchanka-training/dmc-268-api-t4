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
from domain.jobs import (
    LEASE_SECONDS,
    MAX_IN_FLIGHT_PER_INSTALLATION,
    JobPage,
    JobQuery,
    JobStep,
    StepRecord,
    StepStatus,
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
                    " pr_title, author_login, head_ref, base_ref, trace_context, status,"
                    " next_attempt_at)"
                    " VALUES (:id, :delivery_id, :provider, :installation_id, :repo_id,"
                    " :repo_full_name, :pr_number, :head_sha, :base_sha, :event_action,"
                    " :pr_title, :author_login, :head_ref, :base_ref,"
                    " CAST(:trace_context AS JSONB), 'QUEUED', now())"
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
                    "trace_context": (
                        None if job.trace_context is None else json.dumps(dict(job.trace_context))
                    ),
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
                    " pr_title, author_login, head_ref, base_ref, trace_context, retry_count,"
                    # RETURNING sees the updated row: next_attempt_at is still the
                    # value that made the job claimable, started_at is now().
                    " created_at, next_attempt_at, started_at"
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
            enqueued_at=row.created_at,
            claimable_at=row.next_attempt_at,
            claimed_at=row.started_at,
        )

    async def complete(
        self, job_id: str, stats: JobStats, *, worker_id: str, summary: str | None = None
    ) -> bool:
        row_id = _as_uuid(job_id)
        if row_id is None:
            return False
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    "UPDATE pr_review_jobs"
                    " SET status = 'COMPLETED', stats = CAST(:stats AS jsonb),"
                    " summary = :summary, finished_at = now()"
                    " WHERE id = :id AND status = 'PROCESSING' AND locked_by = :worker"
                ),
                {
                    "stats": _stats_to_json(stats),
                    "summary": summary,
                    "id": row_id,
                    "worker": worker_id,
                },
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

    async def reap_expired(self) -> int:
        """Recover jobs whose worker died mid-flight (docs/WORKFLOW_DESIGN.md
        §6; docs/db_models_and_migrations.md §5 "Reap") — the only path that
        can increment retry_count after a worker dies.

        A concrete method of the store, not the domain port: the reaper is
        queue operations (docs/BACKEND_ARCHITECTURE.md § Planned code
        layout, ``worker/reaper`` + ``repository: claim and reap
        statements``), and the in-memory store has no leases to lose.

        The statement is pure literals — no bound parameters at all, so no
        reused-bind ambiguity. Every SET expression evaluates the OLD row
        (PostgreSQL semantics), so the status CASE, the LEAST cap and the
        power(2, …) backoff all see the pre-increment retry_count, like
        ``fail``'s backoff (docs/PIPELINE_SPEC.md §4.3: 1/2/4 min — without
        jitter here, matching the doc's reap SQL). ``LEAST(retry_count + 1,
        max_retries)`` keeps ck_pr_review_jobs_retry_bounds satisfied on
        the FAILED branch. ``finished_at`` on FAILED is an addition over
        the doc SQL for parity with ``fail()``. Returns how many rows were
        reaped.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    "UPDATE pr_review_jobs"
                    " SET status = CASE WHEN retry_count + 1 > max_retries"
                    " THEN 'FAILED' ELSE 'RETRYING' END,"
                    " retry_count = LEAST(retry_count + 1, max_retries),"
                    " next_attempt_at = now() +"
                    " make_interval(secs => least(60 * power(2, retry_count), 900)),"
                    " error_kind = COALESCE(error_kind, 'lease_expired'),"
                    " locked_by = NULL, locked_until = NULL,"
                    " finished_at = CASE WHEN retry_count + 1 > max_retries"
                    " THEN now() ELSE finished_at END"
                    " WHERE status = 'PROCESSING' AND locked_until < now()"
                    " RETURNING id"
                )
            )
            return len(result.all())

    async def record_periodic_run(self, task: str, interval_seconds: float) -> None:
        """A periodic task's success, by the leader that ran it (``periodic_task_runs``,
        migration 0005), stamped with the database's clock."""
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO periodic_task_runs (task, last_success_at, interval_seconds)"
                    " VALUES (:task, now(), :interval)"
                    " ON CONFLICT (task) DO UPDATE"
                    " SET last_success_at = now(), interval_seconds = EXCLUDED.interval_seconds"
                ),
                {"task": task, "interval": float(interval_seconds)},
            )

    async def periodic_runs(self) -> tuple[PeriodicTaskRun, ...]:
        """Every periodic task's last success, for the API's DB sampler."""
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT task, last_success_at, interval_seconds FROM periodic_task_runs"
                    " ORDER BY task"
                )
            )
            return tuple(
                PeriodicTaskRun(row.task, row.last_success_at, float(row.interval_seconds))
                for row in rows
            )

    async def queue_snapshot(self) -> QueueSnapshot:
        """Queue depth for the API's DB sampler, every 15 s. Each filter is the
        predicate of a partial index (``ix_pr_review_jobs_active_pr`` for the counts,
        ``ix_pr_review_jobs_claim`` for the age), so neither reads the job history."""
        async with self._engine.connect() as conn:
            counts = await conn.execute(
                text(
                    "SELECT status, count(*) AS jobs FROM pr_review_jobs"
                    " WHERE status IN ('QUEUED','RETRYING','PROCESSING')"
                    " GROUP BY status"
                )
            )
            by_status = {ReviewJobStatus(row.status): int(row.jobs) for row in counts}
            age = await conn.scalar(
                text(
                    "SELECT EXTRACT(EPOCH FROM now() - min(next_attempt_at))"
                    " FROM pr_review_jobs"
                    " WHERE status IN ('QUEUED','RETRYING') AND next_attempt_at <= now()"
                )
            )
        return QueueSnapshot(
            jobs=tuple((status, by_status.get(status, 0)) for status in QUEUE_STATUSES),
            oldest_claimable_age_seconds=0.0 if age is None else float(age),
        )

    async def get(self, job_id: str) -> ReviewJob | None:
        row_id = _as_uuid(job_id)
        if row_id is None:
            return None
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(f"SELECT {_JOB_COLUMNS} FROM pr_review_jobs WHERE id = :id"),
                {"id": row_id},
            )
            row = result.first()
        return None if row is None else _job_from_row(row)

    async def list_page(self, query: JobQuery) -> JobPage:
        """Newest first, keyset-paged on (created_at, id)."""
        if not query.installation_ids:
            return JobPage(items=(), next_after=None)
        where = ["installation_id = ANY(:installations)"]
        params: dict[str, Any] = {
            "installations": sorted(query.installation_ids),
            "limit": query.limit + 1,
        }
        if query.statuses:
            where.append("status = ANY(:statuses)")
            params["statuses"] = sorted(str(status) for status in query.statuses)
        if query.repository is not None:
            where.append("repo_full_name = :repository")
            params["repository"] = query.repository
        if query.author is not None:
            where.append("author_login = :author")
            params["author"] = query.author
        if query.created_from is not None:
            where.append("created_at >= :created_from")
            params["created_from"] = query.created_from
        if query.created_to is not None:
            where.append("created_at <= :created_to")
            params["created_to"] = query.created_to
        if query.text is not None:
            number = query.text.strip().lstrip("#")
            match = "pr_title ILIKE :title ESCAPE '\\'"
            if number.isdigit():
                match = f"({match} OR pr_number = :number)"
                params["number"] = int(number)
            where.append(match)
            params["title"] = "%" + _escape_like(query.text) + "%"
        if query.after is not None:
            after_id = _as_uuid(query.after[1])
            if after_id is None:
                return JobPage(items=(), next_after=None)
            where.append("(created_at, id) < (:after_at, :after_id)")
            params["after_at"] = query.after[0]
            params["after_id"] = after_id
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    f"SELECT {_JOB_COLUMNS} FROM pr_review_jobs"
                    f" WHERE {' AND '.join(where)}"
                    " ORDER BY created_at DESC, id DESC LIMIT :limit"
                ),
                params,
            )
            rows = result.all()
        jobs = tuple(_job_from_row(row) for row in rows[: query.limit])
        more = len(rows) > query.limit
        return JobPage(
            items=jobs,
            next_after=(jobs[-1].created_at, jobs[-1].job_id) if more else None,
        )

    async def steps(self, job_id: str) -> tuple[StepRecord, ...]:
        row_id = _as_uuid(job_id)
        if row_id is None:
            return ()
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT step, attempt, status, started_at, finished_at, error_kind"
                    " FROM pr_review_steps WHERE job_id = :id ORDER BY attempt, seq"
                ),
                {"id": row_id},
            )
            rows = result.all()
        return tuple(
            StepRecord(
                step=JobStep(row.step),
                attempt=int(row.attempt),
                status=StepStatus(row.status),
                started_at=row.started_at,
                finished_at=row.finished_at,
                error_kind=row.error_kind,
            )
            for row in rows
        )


_JOB_COLUMNS = (
    "id, delivery_id, provider, installation_id, repo_id, repo_full_name, pr_number,"
    " head_sha, base_sha, event_action, pr_title, author_login, head_ref, base_ref,"
    " status, stats, error_kind, created_at, started_at, finished_at, retry_count,"
    " next_attempt_at, summary"
)


def _job_from_row(row: Row[Any]) -> ReviewJob:
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
        started_at=row.started_at,
        summary=row.summary,
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
        # Only the claim reads it; the other queries' rows have no such column.
        trace_context=row._mapping.get("trace_context"),
    )


def _stats_to_json(stats: JobStats) -> str:
    return json.dumps(
        {
            "files_total": stats.files_total,
            "files_reviewable": stats.files_reviewable,
            "hunks_total": stats.hunks_total,
            "chunks_total": stats.chunks_total,
            "skipped_counts": [[reason, count] for reason, count in stats.skipped_counts],
            "findings_published": stats.findings_published,
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
        findings_published=int(value.get("findings_published", 0)),
    )
