# Database Models & Migrations

Persistence layer for the PR Review bot: SQLAlchemy 2.0 **async** ORM models, the
Alembic `env.py` wiring async migrations need, and the initial migration.

The `pr_review_jobs` table serves two roles at once — it is the **work queue** and
the **audit log**. Every schema decision below follows from that: partial indexes
sized to the queue rather than the history, a lease pair for crash recovery, and
terminal rows that are never mutated again.

---

## 1. SQLAlchemy ORM Models

`adapters/db/models.py`

```python
import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class ReviewStatus(str, PyEnum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"
    SKIPPED = "SKIPPED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = frozenset({
    ReviewStatus.COMPLETED, ReviewStatus.FAILED,
    ReviewStatus.SUPERSEDED, ReviewStatus.SKIPPED,
})

ACTIVE_STATUSES = ("QUEUED", "RETRYING", "PROCESSING")
CLAIMABLE_STATUSES = ("QUEUED", "RETRYING")


class Base(DeclarativeBase):
    pass


class ReviewJobORM(Base):
    __tablename__ = "pr_review_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # --- Idempotency -----------------------------------------------------
    # Nullable: manual re-runs have no webhook delivery. PostgreSQL permits
    # unlimited NULLs under a UNIQUE constraint, so this is safe.
    delivery_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # --- GitHub coordinates ----------------------------------------------
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repo_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repo_full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    event_action: Mapped[str] = mapped_column(String(32), nullable=False)

    # --- Lifecycle -------------------------------------------------------
    # VARCHAR + CHECK, not a native PG ENUM: ALTER TYPE ... ADD VALUE cannot
    # run inside a transaction on PG < 12 and values can never be removed.
    # This set will grow.
    status: Mapped[ReviewStatus] = mapped_column(
        String(16), nullable=False,
        default=ReviewStatus.QUEUED, server_default="QUEUED",
    )

    # --- Queue mechanics -------------------------------------------------
    locked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_retries: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )

    # --- Outcome ---------------------------------------------------------
    check_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_log: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # server_default, not a Python default: the worker claims and reaps jobs
    # with raw UPDATE statements that never pass through the ORM.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    # lazy="raise_on_sql": under asyncio an implicit lazy load raises
    # MissingGreenlet at runtime anyway. This turns that into a loud,
    # deterministic error at development time, and stops every queue poll
    # from dragging comment rows along. Load explicitly with
    # selectinload(ReviewJobORM.comments) where they are actually needed.
    comments: Mapped[list["ReviewCommentORM"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="raise_on_sql",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED','PROCESSING','RETRYING','COMPLETED',"
            "'FAILED','SUPERSEDED','SKIPPED')",
            name="ck_pr_review_jobs_status",
        ),
        CheckConstraint(
            "retry_count >= 0 AND retry_count <= max_retries",
            name="ck_pr_review_jobs_retry_bounds",
        ),
        UniqueConstraint("delivery_id", name="uq_pr_review_jobs_delivery_id"),
        # Queue claim path. Partial, so the index covers only in-flight work:
        # a plain index on `status` would be a b-tree over a column that is
        # ~99% 'COMPLETED' and would grow forever with the audit log.
        Index(
            "ix_pr_review_jobs_claim", "next_attempt_at",
            postgresql_where=text("status IN ('QUEUED','RETRYING')"),
        ),
        # Supersession lookup on enqueue.
        Index(
            "ix_pr_review_jobs_active_pr", "repo_id", "pr_number",
            postgresql_where=text("status IN ('QUEUED','RETRYING','PROCESSING')"),
        ),
        # Reaper scan for expired leases.
        Index(
            "ix_pr_review_jobs_reaper", "locked_until",
            postgresql_where=text("status = 'PROCESSING'"),
        ),
        # "Review history for this PR" — also serves the layer-2 idempotency
        # check for a recent COMPLETED job at the same head SHA.
        Index(
            "ix_pr_review_jobs_pr_history",
            "repo_id", "pr_number", text("created_at DESC"),
        ),
    )


class ReviewCommentORM(Base):
    __tablename__ = "pr_review_comments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pr_review_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )

    # sha256 over file_path + normalized content + anchored snippet.
    # Deliberately excludes line_number: lines shift when unrelated code
    # above them changes, and a line-keyed fingerprint would re-post every
    # finding in a file after a one-line insertion at the top.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    # Nullable: file-level and PR-level findings have no line.
    line_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    side: Mapped[str | None] = mapped_column(String(5), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # NULL until the comment actually reaches GitHub. Written per-comment as
    # it is posted, not batched at the end — a crash between posting and
    # persisting is what causes duplicate comments on retry.
    external_comment_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    job: Mapped["ReviewJobORM"] = relationship(back_populates="comments")

    __table_args__ = (
        CheckConstraint("side IS NULL OR side IN ('LEFT','RIGHT')",
                        name="ck_pr_review_comments_side"),
        # Guards against double-insert within a job. Its leading column also
        # serves the foreign key, so no separate index on job_id is needed —
        # PostgreSQL does NOT auto-index foreign keys, and without one every
        # cascade delete and comment load would sequentially scan.
        UniqueConstraint("job_id", "fingerprint",
                         name="uq_pr_review_comments_job_fingerprint"),
    )
```

---

## 2. Async Engine & Session

`adapters/db/session.py`

```python
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine(
    settings.database_url,          # postgresql+asyncpg://...
    pool_size=settings.worker_concurrency + 2,
    pool_pre_ping=True,
    echo=False,
)

SessionFactory = async_sessionmaker(engine, expire_on_commit=False)
```

`expire_on_commit=False` is not optional under asyncio: with the default, touching
any attribute after `commit()` triggers a refresh, which is an implicit lazy load
and raises `MissingGreenlet`.

The API process and the worker process each build their own engine. Pool size
follows worker concurrency rather than CPU count — connections are held only
briefly, because the LLM call happens outside any transaction.

---

## 3. Alembic `env.py` (async)

Alembic's default template is synchronous and will not drive an async engine.

```python
import asyncio
from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool

from adapters.db.models import Base

target_metadata = Base.metadata


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
```

`compare_server_default=True` matters here — several columns rely on server
defaults, and without it autogenerate silently ignores drift in them.

---

## 4. Alembic Initial Migration

`alembic/versions/0001_initial_schema.py`

```python
"""Initial PR review schema

Revision ID: 0001
Revises:
Create Date: 2026-09-08 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATUSES = (
    "'QUEUED','PROCESSING','RETRYING','COMPLETED',"
    "'FAILED','SUPERSEDED','SKIPPED'"
)


def upgrade() -> None:
    op.create_table(
        "pr_review_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("delivery_id", sa.String(length=36), nullable=True),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("repo_id", sa.BigInteger(), nullable=False),
        sa.Column("repo_full_name", sa.String(length=255), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(length=40), nullable=False),
        sa.Column("base_sha", sa.String(length=40), nullable=False),
        sa.Column("event_action", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="QUEUED"),
        sa.Column("locked_by", sa.String(length=255), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("now()")),
        sa.Column("retry_count", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("max_retries", sa.Integer(), nullable=False,
                  server_default="3"),
        sa.Column("check_run_id", sa.BigInteger(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("error_kind", sa.String(length=32), nullable=True),
        sa.Column("error_log", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_id", name="uq_pr_review_jobs_delivery_id"),
        sa.CheckConstraint(f"status IN ({STATUSES})",
                           name="ck_pr_review_jobs_status"),
        sa.CheckConstraint("retry_count >= 0 AND retry_count <= max_retries",
                           name="ck_pr_review_jobs_retry_bounds"),
    )

    # Partial indexes: each covers only rows in the relevant state, so the
    # queue indexes stay small no matter how large the audit history grows.
    op.create_index(
        "ix_pr_review_jobs_claim", "pr_review_jobs", ["next_attempt_at"],
        postgresql_where=sa.text("status IN ('QUEUED','RETRYING')"),
    )
    op.create_index(
        "ix_pr_review_jobs_active_pr", "pr_review_jobs",
        ["repo_id", "pr_number"],
        postgresql_where=sa.text(
            "status IN ('QUEUED','RETRYING','PROCESSING')"),
    )
    op.create_index(
        "ix_pr_review_jobs_reaper", "pr_review_jobs", ["locked_until"],
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )
    op.create_index(
        "ix_pr_review_jobs_pr_history", "pr_review_jobs",
        ["repo_id", "pr_number", sa.text("created_at DESC")],
    )

    op.create_table(
        "pr_review_comments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=True),
        sa.Column("start_line", sa.Integer(), nullable=True),
        sa.Column("side", sa.String(length=5), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("external_comment_id", sa.BigInteger(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["job_id"], ["pr_review_jobs.id"],
                                ondelete="CASCADE"),
        sa.UniqueConstraint("job_id", "fingerprint",
                            name="uq_pr_review_comments_job_fingerprint"),
        sa.CheckConstraint("side IS NULL OR side IN ('LEFT','RIGHT')",
                           name="ck_pr_review_comments_side"),
    )

    # updated_at maintained by trigger, not by ORM onupdate: the claim and
    # reaper paths are raw UPDATE statements that never pass through the ORM.
    op.execute("""
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_pr_review_jobs_updated_at
        BEFORE UPDATE ON pr_review_jobs
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_pr_review_jobs_updated_at "
               "ON pr_review_jobs;")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at();")
    # Dropping a table drops its own indexes and constraints.
    op.drop_table("pr_review_comments")
    op.drop_table("pr_review_jobs")
```

---

## 5. Repository Query Reference

The three statements that carry the design. All are single statements, so no state
is lost to a crash between steps.

**Enqueue with supersession** — one transaction with the `INSERT`:

```sql
UPDATE pr_review_jobs SET status = 'SUPERSEDED', finished_at = now()
 WHERE repo_id = :repo_id AND pr_number = :pr_number
   AND status IN ('QUEUED','RETRYING','PROCESSING');
```

**Claim** — commits immediately; the review runs outside the transaction, protected
by `locked_until` rather than by the row lock:

```sql
UPDATE pr_review_jobs
   SET status = 'PROCESSING', locked_by = :worker_id,
       locked_until = now() + :lease, started_at = now()
 WHERE id = (SELECT id FROM pr_review_jobs
              WHERE status IN ('QUEUED','RETRYING')
                AND next_attempt_at <= now()
              ORDER BY next_attempt_at
              FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING *;
```

**Reap** — the only path that can increment `retry_count` after a worker dies:

```sql
UPDATE pr_review_jobs
   SET status = CASE WHEN retry_count + 1 > max_retries
                     THEN 'FAILED' ELSE 'RETRYING' END,
       retry_count = retry_count + 1,
       next_attempt_at = now() + (interval '1 minute' * power(2, retry_count)),
       error_kind = COALESCE(error_kind, 'lease_expired'),
       locked_by = NULL, locked_until = NULL
 WHERE status = 'PROCESSING' AND locked_until < now();
```

---

## 6. Operational Notes

* **Table growth.** `pr_review_jobs` is append-mostly and unbounded. The partial
  indexes keep queue operations flat, but the table itself needs a retention policy
  — monthly partitioning on `created_at`, or a purge of terminal rows older than N
  days. Decide before the first busy month, not after.
* **`updated_at` on `pr_review_comments`.** Absent by design: comment rows are
  written once and only patched with `external_comment_id`/`posted_at`. If comments
  become editable, add the column and extend the trigger.
* **Autovacuum.** Jobs are updated 3–5 times each across their lifecycle, so the
  table accumulates dead tuples faster than its insert rate suggests. Consider a
  lowered `autovacuum_vacuum_scale_factor` on this table specifically.
* **`LISTEN`/`NOTIFY`.** Requires a dedicated asyncpg connection outside the pool.
  It is a latency optimisation only — correctness rests on the polling fallback, so
  a missed notification must never strand a job.
