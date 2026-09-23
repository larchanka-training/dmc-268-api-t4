# Database Models & Migrations

Persistence layer for the PR Review bot: SQLAlchemy 2.0 **async** ORM models, the
Alembic `env.py` wiring async migrations need, and the initial migration.

Two groups of tables. The **review domain** (§1.1) is where the design pressure
is: the queue, the lease, the fingerprints. The **tenancy, billing and auth**
tables (§1.2) are comparatively ordinary, and their rationale lives in
[BACKEND_ARCHITECTURE.md](./BACKEND_ARCHITECTURE.md) rather than being repeated
here. Configuration tables are defined in [configuration.md §1](./configuration.md).

The `pr_review_jobs` table serves two roles at once — it is the **work queue** and
the **audit log**. Every schema decision below follows from that: partial indexes
sized to the queue rather than the history, a lease pair for crash recovery, and
terminal rows that are never mutated again.

---

## 1. SQLAlchemy ORM Models

### 1.1 Review domain

`adapters/db/models.py`

```python
import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
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

    # --- Forge coordinates ------------------------------------------------
    # Scopes every identifier below: repo_id 42 on GitHub and 42 on GitLab are
    # different repositories, so no lookup keys on repo_id alone.
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    # GitHub: installation id. GitLab: the project/group credential reference.
    # FK to our installations row rather than the forge's numeric id: an
    # installation already knows its provider and its credential, so one column
    # carries both and a retry hours later re-authenticates from the database.
    installation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("installations.id"), nullable=False
    )
    # Denormalised from installations.account_id so the dashboard's main query
    # ("my jobs, newest first") needs no join. Safe to denormalise because an
    # installation never moves between accounts; if that ever changes, this
    # column becomes a backfill.
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False
    )
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

    # Who caused this review, and what the forge says about their standing.
    # Attribution (whose PRs spent the account's quota), per-author rate
    # limiting, and the automatic-review gate all read these.
    # Nullable for the same reason as the SHAs: a comment payload carries the
    # commenter, not the PR author, so a command job resolves it at claim.
    author_external_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    # Normalised, not the forge's vocabulary - orgs are a non-goal, so GitHub's
    # MEMBER and its three CONTRIBUTOR variants collapse. GitHub OWNER maps to
    # OWNER; MEMBER and COLLABORATOR to COLLABORATOR; the CONTRIBUTOR family to
    # CONTRIBUTOR; NONE and MANNEQUIN to NONE. GitLab maps Owner to OWNER,
    # Maintainer and Developer to COLLABORATOR, Reporter and Guest to
    # CONTRIBUTOR, non-members to NONE.
    author_association: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )

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
    # GitHub Check Run id, or GitLab MR note id — same role, one column.
    external_check_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    # Audit only: which settings governed this review. Deliberately NOT part
    # of the dedupe lookup - config changes must never repost comments the
    # reader has already seen. See configuration.md.
    config_digest: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("config_snapshots.digest"), nullable=True
    )
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
        CheckConstraint(
            "event_action = 'command' "
            "OR (head_sha IS NOT NULL AND base_sha IS NOT NULL)",
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
        # drawn from different namespaces and must not collide into one another.
        UniqueConstraint("provider", "delivery_id",
                         name="uq_pr_review_jobs_delivery_id"),
        # Queue claim path. Partial, so the index covers only in-flight work:
        # a plain index on `status` would be a b-tree over a column that is
        # ~99% 'COMPLETED' and would grow forever with the audit log.
        Index(
            "ix_pr_review_jobs_claim", "next_attempt_at",
            postgresql_where=text("status IN ('QUEUED','RETRYING')"),
        ),
        # Supersession lookup on enqueue.
        Index(
            "ix_pr_review_jobs_active_pr",
            "installation_id", "repo_id", "pr_number",
            postgresql_where=text("status IN ('QUEUED','RETRYING','PROCESSING')"),
        ),
        # Reaper scan for expired leases.
        Index(
            "ix_pr_review_jobs_reaper", "locked_until",
            postgresql_where=text("status = 'PROCESSING'"),
        ),
        # Per-account in-flight count, read on every claim. Partial, so it
        # covers only work in progress rather than the whole audit log.
        Index(
            "ix_pr_review_jobs_inflight", "account_id",
            postgresql_where=text("status = 'PROCESSING'"),
        ),
        # "Review history for this PR" — also serves the layer-2 idempotency
        # check for a recent COMPLETED job at the same head SHA.
        Index(
            "ix_pr_review_jobs_pr_history",
            "installation_id", "repo_id", "pr_number", text("created_at DESC"),
        ),
        # The dashboard listing. Leads with account_id, so keyset pagination on
        # created_at is an index scan rather than a join and a sort.
        Index(
            "ix_pr_review_jobs_account_history",
            "account_id", text("created_at DESC"),
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


class ReviewStepORM(Base):
    """One row per pipeline stage per attempt. Diagnosis, not audit."""

    __tablename__ = "pr_review_steps"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pr_review_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The job's retry_count when this step ran. A retried job writes a fresh
    # set of rows, so "attempt 1 died at LLM_CALL, attempt 2 passed" reads off
    # the table without modelling retries as a special case.
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    step: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="RUNNING"
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Null while RUNNING. Inserted at start and updated at finish rather than
    # written once at the end: a step that hangs must still leave a row, since
    # a hanging step is precisely what this table exists to diagnose.
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    error_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Bounded so no payload can fit, and passed through the redactor before
    # it is written - an upstream error can echo part of the request.
    error_detail: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Digests and counts only. Never prompt, diff or response content:
    #   {"model": "...", "input_tokens": 41233, "prompt_digest": "9f2c...",
    #    "files": 18, "truncated": true, "redacted": {"aws-access-key": 1}}
    metrics: Mapped[dict] = mapped_column(
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
        # job_id - the same argument as pr_review_comments above.
        UniqueConstraint("job_id", "attempt", "seq",
                         name="uq_pr_review_steps_job_attempt_seq"),
        # Latency aggregates per stage, e.g. p95 of LLM_CALL this week.
        Index("ix_pr_review_steps_step_started", "step", "started_at"),
    )
```

### 1.2 Tenancy, billing and auth

`adapters/db/tenancy.py`, `billing.py`, `auth.py`. Abbreviated to the columns and
constraints that carry a decision: every `id` is `UUID(as_uuid=True)` with a
`uuid4` default, and every `created_at` a `server_default=text("now()")`.
`LargeBinary` joins the §1.1 imports.

```python
class AccountORM(Base):
    __tablename__ = "accounts"
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True        # null until first checkout
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        server_default="active")
    __table_args__ = (
        CheckConstraint("status IN ('active','suspended','closed')",
                        name="ck_accounts_status"),
    )


class UserORM(Base):
    __tablename__ = "users"
    # Notices only. Never an identity key, never a join condition: auto-linking
    # by email is an account takeover path (BACKEND_ARCHITECTURE.md, Tenancy).
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)


class IdentityORM(Base):
    __tablename__ = "identities"
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_username: Mapped[str] = mapped_column(String(255), nullable=False)
    __table_args__ = (
        # One person, many forge logins. Scoped per provider because user id 1
        # exists on both.
        UniqueConstraint("provider", "provider_user_id",
                         name="uq_identities_provider_user"),
        CheckConstraint("provider IN ('github','gitlab')",
                        name="ck_identities_provider"),
    )


class AccountMemberORM(Base):
    __tablename__ = "account_members"
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # One value today. The CHECK already permits the others, so adding a role
    # later is a data change rather than a migration.
    role: Mapped[str] = mapped_column(String(16), nullable=False,
                                      server_default="owner")
    __table_args__ = (
        CheckConstraint("role IN ('owner','admin','member')",
                        name="ck_account_members_role"),
    )


class InstallationORM(Base):
    __tablename__ = "installations"
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    external_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_login: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        server_default="active")
    __table_args__ = (
        UniqueConstraint("provider", "external_id",
                         name="uq_installations_provider_external"),
        CheckConstraint("provider IN ('github','gitlab')",
                        name="ck_installations_provider"),
        CheckConstraint("status IN ('active','suspended','revoked')",
                        name="ck_installations_status"),
    )


class ProviderCredentialORM(Base):
    __tablename__ = "provider_credentials"
    # GitLab only. GitHub derives short-lived tokens from our own private key
    # and needs nothing stored. The sanctioned exception to "no secrets in the
    # database" (configuration.md).
    installation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("installations.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)   # which KEK
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SubscriptionORM(Base):
    __tablename__ = "subscriptions"
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    stripe_subscription_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    plan: Mapped[str] = mapped_column(String(16), nullable=False)
    # Our five states, not Stripe's wider vocabulary.
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    stripe_status_raw: Mapped[str] = mapped_column(String(32), nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    grace_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Stripe delivers events out of order. A write applies only when the event
    # is newer than this, or a delayed update resurrects a cancelled plan.
    last_event_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    __table_args__ = (
        CheckConstraint(
            "status IN ('TRIALING','ACTIVE','GRACE','SUSPENDED','CANCELLED')",
            name="ck_subscriptions_status"),
        CheckConstraint("plan IN ('free','pro','max')",
                        name="ck_subscriptions_plan"),
    )


class PlanPriceORM(Base):
    __tablename__ = "plan_prices"
    # Plan to Stripe price id, so a price change is data rather than a deploy.
    plan: Mapped[str] = mapped_column(String(16), primary_key=True)
    stripe_price_id: Mapped[str] = mapped_column(String(64), nullable=False)


class StripeEventORM(Base):
    __tablename__ = "stripe_events"
    # Idempotency for at-least-once delivery, mirroring delivery_id on jobs.
    stripe_event_id: Mapped[str] = mapped_column(String(64), nullable=False,
                                                 unique=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  nullable=False)


class UsageRecordORM(Base):
    __tablename__ = "usage_records"
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    # One row per completed review. UNIQUE so a retried metering write cannot
    # bill the same review twice.
    # SET NULL, not CASCADE: jobs are purged after 30 days and billing records
    # must outlive what they billed for. After a purge the row keeps its
    # account, period and token counts with a null job_id - and PostgreSQL
    # permits unlimited NULLs under UNIQUE, so the constraint still holds.
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pr_review_jobs.id", ondelete="SET NULL"),
        nullable=True, unique=True,
    )
    # Start of the Stripe billing period this review falls in, so usage and
    # the invoice always describe the same window. Free accounts have no
    # subscription, so their period is the account anniversary.
    period: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                             nullable=False)
    # The PR this review belongs to, stored here and not reached through the
    # job: quota counts DISTINCT pull requests, and job rows are purged at 30
    # days. Without these columns an invoice could not be reconstructed once
    # job_id went null.
    repo_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (
        # The quota check counts distinct (repo_id, pr_number) for one account
        # in the current period, so the index carries them too.
        Index("ix_usage_records_account_period",
              "account_id", "period", "repo_id", "pr_number"),
    )


class SessionORM(Base):
    __tablename__ = "sessions"
    # PK is the SHA-256 of the cookie value; the raw token is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Which login opened this session, so an audit trail survives two identities.
    identity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identities.id", ondelete="CASCADE"),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 nullable=False)
    __table_args__ = (Index("ix_sessions_expires_at", "expires_at"),)


class ApiKeyORM(Base):
    __tablename__ = "api_keys"
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Look up by prefix, then verify the hash; the secret is never stored.
    prefix: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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

**`0001` is autogenerated and then hand-edited**, which is the normal workflow and
the reason only part of it is reproduced here.

| | Produced by | Why |
| :--- | :--- | :--- |
| §1.2 tables (tenancy, billing, auth) | `alembic revision --autogenerate` | Ordinary tables. Autogenerate emits columns, PKs, FKs, unique and check constraints correctly, and sorts `create_table` calls into foreign-key order by itself |
| §1.1 tables (review domain) | Hand-written, below | Autogenerate cannot see the `updated_at` trigger at all, and does not reliably emit the `created_at DESC` expression indexes |

**Every model module must be imported before autogenerate runs.** Alembic compares
`Base.metadata` to the live database, so a model it has not imported looks like a
table that should be *dropped*. `adapters/db/__init__.py` imports all of them —
`models`, `tenancy`, `billing`, `auth` and `config` — for exactly this reason, and
that import is load-bearing rather than tidy.

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
    # --- autogenerated above this line -------------------------------------
    # accounts, users, identities, account_members, installations,
    # provider_credentials, subscriptions, plan_prices, stripe_events,
    # sessions, api_keys, config_overrides, config_snapshots.
    # They must exist first: pr_review_jobs references installations,
    # accounts and config_snapshots.
    #
    # usage_records is the exception - it references pr_review_jobs, so
    # autogenerate places it after the block below.
    # --- hand-written from here --------------------------------------------

    op.create_table(
        "pr_review_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("delivery_id", sa.String(length=36), nullable=True),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("installation_id", postgresql.UUID(as_uuid=True),
                  nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repo_id", sa.BigInteger(), nullable=False),
        sa.Column("repo_full_name", sa.String(length=255), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(length=40), nullable=True),
        sa.Column("base_sha", sa.String(length=40), nullable=True),
        sa.Column("event_action", sa.String(length=32), nullable=False),
        sa.Column("author_external_id", sa.String(length=64), nullable=True),
        sa.Column("author_association", sa.String(length=16), nullable=True),
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
        sa.Column("external_check_id", sa.BigInteger(), nullable=True),
        sa.Column("config_digest", sa.String(length=64), nullable=True),
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
        sa.UniqueConstraint("provider", "delivery_id",
                            name="uq_pr_review_jobs_delivery_id"),
        sa.ForeignKeyConstraint(["installation_id"], ["installations.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["config_digest"],
                                ["config_snapshots.digest"]),
        sa.CheckConstraint("provider IN ('github','gitlab')",
                           name="ck_pr_review_jobs_provider"),
        sa.CheckConstraint(f"status IN ({STATUSES})",
                           name="ck_pr_review_jobs_status"),
        sa.CheckConstraint("retry_count >= 0 AND retry_count <= max_retries",
                           name="ck_pr_review_jobs_retry_bounds"),
        sa.CheckConstraint("event_action = 'command' "
                           "OR (head_sha IS NOT NULL AND base_sha IS NOT NULL)",
                           name="ck_pr_review_jobs_sha_present"),
        sa.CheckConstraint("author_association IS NULL OR author_association IN "
                           "('OWNER','COLLABORATOR','CONTRIBUTOR','NONE')",
                           name="ck_pr_review_jobs_author_association"),
    )

    # Partial indexes: each covers only rows in the relevant state, so the
    # queue indexes stay small no matter how large the audit history grows.
    op.create_index(
        "ix_pr_review_jobs_claim", "pr_review_jobs", ["next_attempt_at"],
        postgresql_where=sa.text("status IN ('QUEUED','RETRYING')"),
    )
    op.create_index(
        "ix_pr_review_jobs_active_pr", "pr_review_jobs",
        ["installation_id", "repo_id", "pr_number"],
        postgresql_where=sa.text(
            "status IN ('QUEUED','RETRYING','PROCESSING')"),
    )
    op.create_index(
        "ix_pr_review_jobs_reaper", "pr_review_jobs", ["locked_until"],
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )
    op.create_index(
        "ix_pr_review_jobs_inflight", "pr_review_jobs", ["account_id"],
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )
    op.create_index(
        "ix_pr_review_jobs_pr_history", "pr_review_jobs",
        ["installation_id", "repo_id", "pr_number", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_pr_review_jobs_account_history", "pr_review_jobs",
        ["account_id", sa.text("created_at DESC")],
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

    op.create_table(
        "pr_review_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("step", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="RUNNING"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_kind", sa.String(length=32), nullable=True),
        sa.Column("error_detail", sa.String(length=1000), nullable=True),
        sa.Column("metrics", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["job_id"], ["pr_review_jobs.id"],
                                ondelete="CASCADE"),
        sa.UniqueConstraint("job_id", "attempt", "seq",
                            name="uq_pr_review_steps_job_attempt_seq"),
        sa.CheckConstraint(
            "step IN ('CLAIMED','AUTH','FETCH_DIFF','BUILD_CONTEXT',"
            "'REDACT','GATE','LLM_CALL','POSTPROCESS','POST_FEEDBACK',"
            "'METER')",
            name="ck_pr_review_steps_step"),
        sa.CheckConstraint("status IN ('RUNNING','OK','FAILED','SKIPPED')",
                           name="ck_pr_review_steps_status"),
    )
    op.create_index(
        "ix_pr_review_steps_step_started", "pr_review_steps",
        ["step", "started_at"],
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
    # Reverse of upgrade: dependants first.
    op.execute("DROP TRIGGER IF EXISTS trg_pr_review_jobs_updated_at "
               "ON pr_review_jobs;")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at();")
    # Dropping a table drops its own indexes and constraints. Dependants
    # first; the autogenerated drops for every other table follow below.
    op.drop_table("pr_review_steps")
    op.drop_table("pr_review_comments")
    op.drop_table("usage_records")
    op.drop_table("pr_review_jobs")
```

---

## 5. Repository Query Reference

The three statements that carry the design. All are single statements, so no state
is lost to a crash between steps.

**Enqueue with supersession** — one transaction with the `INSERT`:

```sql
UPDATE pr_review_jobs SET status = 'SUPERSEDED', finished_at = now()
 WHERE installation_id = :installation_id AND repo_id = :repo_id
   AND pr_number = :pr_number
   AND status IN ('QUEUED','RETRYING','PROCESSING');
```

**Claim** — commits immediately; the review runs outside the transaction, protected
by `locked_until` rather than by the row lock. The per-account in-flight filter is
in [WORKFLOW_DESIGN.md §2 Step 4](./WORKFLOW_DESIGN.md); omitted here to keep the
locking shape legible:

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

* **Creation order is a real constraint.** `pr_review_jobs` references
  `installations`, `accounts` and `config_snapshots`, and `usage_records`
  references `pr_review_jobs`. Autogenerate sorts by foreign key, but the
  hand-written block does not move itself — after regenerating, check that the
  review-domain tables still sit after their dependencies.
* **`0001` is still editable.** No database has run it yet, so schema changes
  belong *in* it rather than in a follow-up revision — nullable `head_sha` above
  arrived that way. This stops at the first apply anyone cannot drop, including a
  teammate's local database: Alembic records the revision id, not the file's
  contents, so an edited `0001` leaves an already-migrated database silently
  diverged. After the first real deploy, `0001` is frozen.
* **Table growth.** `pr_review_jobs` is append-mostly and unbounded. The partial
  indexes keep queue operations flat, but the table itself needs a retention policy
  — monthly partitioning on `created_at`, or a purge of terminal rows older than N
  days. Decide before the first busy month, not after.
* **`updated_at` on `pr_review_comments`.** Absent by design: comment rows are
  written once and only patched with `external_comment_id`/`posted_at`. If comments
  become editable, add the column and extend the trigger.
* **The per-author rate limit needs no index of its own.** It counts one
  account's jobs by author over the last day, which `ix_pr_review_jobs_account_history`
  already serves — a day of rows for one account is under a hundred at this
  scale. Add `(installation_id, author_external_id, created_at DESC)` only if
  that count ever shows up in query timings.
* **`pr_review_steps` has its own retention, and it is shorter.** Job rows are
  the audit log; step rows are debugging data whose value collapses after a week
  or two. Purging them independently means the dashboard must render a job whose
  steps have aged out. Row count is roughly 10x the job table, which is nothing
  at tens of reviews a day and the first thing to sample if that changes.
* **`usage_records` is deliberately excluded from the retention purge.** It is
  the billing trail, and accounting retention runs to years rather than days. Its
  `job_id` is `ON DELETE SET NULL` so a purged job leaves the account, period and
  token counts intact. It also preserves pricing optionality: any billing
  dimension — reviews, distinct PRs, tokens — can be re-derived from these rows
  later, including retroactively.
* **`sessions` and `stripe_events` grow forever without help.** An expired
  session is rejected at authentication time but its row is never deleted, and
  `stripe_events` exists purely so a redelivered webhook collapses. Both belong in
  the retention purge — sessions by `expires_at`, events by
  `stripe_event_retention_days`. `pr_review_comments` and `pr_review_steps` need
  no separate statement when a job is purged: both cascade.
* **Autovacuum.** Jobs are updated 3–5 times each across their lifecycle, so the
  table accumulates dead tuples faster than its insert rate suggests. Consider a
  lowered `autovacuum_vacuum_scale_factor` on this table specifically.
* **`LISTEN`/`NOTIFY`.** Requires a dedicated asyncpg connection outside the pool.
  It is a latency optimisation only — correctness rests on the polling fallback, so
  a missed notification must never strand a job.
