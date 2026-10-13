"""SQLAlchemy 2.0 ORM models for the review domain.

The queue's claim and reap paths run raw UPDATE statements that bypass the ORM
entirely, which is why the timestamp columns carry server defaults rather than
Python defaults (docs/db_models_and_migrations.md §1.1, created_at comment).
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ACTIVE_STATUSES = ("QUEUED", "RETRYING", "PROCESSING")
CLAIMABLE_STATUSES = ("QUEUED", "RETRYING")


class ReviewStatus(StrEnum):
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


_TERMINAL = frozenset(
    {
        ReviewStatus.COMPLETED,
        ReviewStatus.FAILED,
        ReviewStatus.SUPERSEDED,
        ReviewStatus.SKIPPED,
    }
)


class Base(DeclarativeBase):
    pass


class ReviewJobORM(Base):
    __tablename__ = "pr_review_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )

    # --- Idempotency -----------------------------------------------------
    # Nullable: manual re-runs have no webhook delivery. PostgreSQL permits
    # unlimited NULLs under a UNIQUE constraint, so this is safe.
    delivery_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # --- Forge coordinates ------------------------------------------------
    # Scopes every identifier below: repo_id 42 on GitHub and 42 on GitLab are
    # different repositories, so no lookup keys on repo_id alone.
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    # DEVIATION from docs/db_models_and_migrations.md §1.1: the docs store a
    # UUID FK into installations; tenancy tables are deferred to a later
    # migration, so the forge's numeric installation id is stored directly.
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repo_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repo_full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Nullable only for command-originated jobs: a comment payload carries no
    # SHAs, so the worker resolves the range at claim (workflow §2, Step 5).
    # ck_pr_review_jobs_sha_present keeps every webhook-originated row strict.
    head_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    base_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # GitHub/GitLab action name, or 'command' for a mention-triggered review.
    event_action: Mapped[str] = mapped_column(String(32), nullable=False)

    # D5 (docs/PIPELINE_SPEC.md §7.2, decided 2026-10-10): PR metadata for the
    # review list; all four values arrive in the same webhook payload.
    pr_title: Mapped[str] = mapped_column(String(255), nullable=False)
    author_login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    head_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    base_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Who caused this review, and what the forge says about their standing.
    # Nullable for the same reason as the SHAs: a comment payload carries the
    # commenter, not the PR author, so a command job resolves it at claim.
    author_external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Normalised, not the forge's vocabulary - orgs are a non-goal, so GitHub's
    # MEMBER and its three CONTRIBUTOR variants collapse (docs §1.1).
    author_association: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # --- Lifecycle -------------------------------------------------------
    # VARCHAR + CHECK, not a native PG ENUM: ALTER TYPE ... ADD VALUE cannot
    # run inside a transaction on PG < 12 and values can never be removed.
    status: Mapped[ReviewStatus] = mapped_column(
        String(16),
        nullable=False,
        default=ReviewStatus.QUEUED,
        server_default="QUEUED",
    )

    # --- Queue mechanics -------------------------------------------------
    locked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")

    # --- Outcome ---------------------------------------------------------
    # GitHub Check Run id, or GitLab MR note id — same role, one column.
    external_check_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Audit only, deliberately not part of the dedupe lookup (docs §1.1).
    # No FK: config_snapshots arrives with the configuration migrations.
    config_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # ADDITION to the docs schema: parsed-diff stats from the worker, added
    # while 0001 was still editable (docs/db_models_and_migrations.md §6).
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_log: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # server_default, not a Python default: the worker claims and reaps jobs
    # with raw UPDATE statements that never pass through the ORM.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
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
        CheckConstraint(
            "event_action = 'command' OR (head_sha IS NOT NULL AND base_sha IS NOT NULL)",
            name="ck_pr_review_jobs_sha_present",
        ),
        CheckConstraint(
            "author_association IS NULL OR author_association IN "
            "('OWNER','COLLABORATOR','CONTRIBUTOR','NONE')",
            name="ck_pr_review_jobs_author_association",
        ),
        CheckConstraint(
            "provider IN ('github','gitlab')",
            name="ck_pr_review_jobs_provider",
        ),
        # Per provider: a GitHub delivery GUID and a GitLab event UUID are
        # drawn from different namespaces and must not collide.
        UniqueConstraint("provider", "delivery_id", name="uq_pr_review_jobs_delivery_id"),
        # Partial, so each index covers only in-flight work: a plain index on
        # status would grow forever with the audit log (docs §1.1).
        Index(
            "ix_pr_review_jobs_claim",
            "next_attempt_at",
            postgresql_where=text("status IN ('QUEUED','RETRYING')"),
        ),
        Index(
            "ix_pr_review_jobs_active_pr",
            "installation_id",
            "repo_id",
            "pr_number",
            postgresql_where=text("status IN ('QUEUED','RETRYING','PROCESSING')"),
        ),
        Index(
            "ix_pr_review_jobs_reaper",
            "locked_until",
            postgresql_where=text("status = 'PROCESSING'"),
        ),
        # DEVIATION from docs §1.1: the docs index account_id; accounts arrive
        # with tenancy, so the MVP keys per-account fairness on installation_id.
        Index(
            "ix_pr_review_jobs_inflight",
            "installation_id",
            postgresql_where=text("status = 'PROCESSING'"),
        ),
        # "Review history for this PR" — also serves the layer-2 idempotency
        # check for a recent COMPLETED job at the same head SHA.
        Index(
            "ix_pr_review_jobs_pr_history",
            "installation_id",
            "repo_id",
            "pr_number",
            text("created_at DESC"),
        ),
    )


class ReviewStepORM(Base):
    """One row per pipeline stage per attempt. Diagnosis, not audit."""

    __tablename__ = "pr_review_steps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pr_review_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The job's retry_count when this step ran. A retried job writes a fresh
    # set of rows, so "attempt 1 died at LLM_CALL, attempt 2 passed" reads off
    # the table without modelling retries as a special case.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    step: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="RUNNING")

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Null while RUNNING. Inserted at start and updated at finish rather than
    # written once at the end: a step that hangs must still leave a row.
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    error_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Bounded so no payload can fit, and passed through the redactor before it
    # is written - an upstream error can echo part of the request.
    error_detail: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Digests and counts only. Never prompt, diff or response content.
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        CheckConstraint(
            "step IN ('CLAIMED','AUTH','FETCH_DIFF','BUILD_CONTEXT',"
            "'REDACT','GATE','LLM_CALL','POSTPROCESS','POST_FEEDBACK',"
            "'METER')",
            name="ck_pr_review_steps_step",
        ),
        CheckConstraint(
            "status IN ('RUNNING','OK','FAILED','SKIPPED')",
            name="ck_pr_review_steps_status",
        ),
        # Leading column serves the foreign key too, so no separate index on
        # job_id (docs §1.1).
        UniqueConstraint("job_id", "attempt", "seq", name="uq_pr_review_steps_job_attempt_seq"),
        # Latency aggregates per stage, e.g. p95 of LLM_CALL this week.
        Index("ix_pr_review_steps_step_started", "step", "started_at"),
    )
