import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from domain.auth import Session, SignInAttempt
from domain.models import ReviewResult
from domain.repositories import Repository
from domain.tenancy import Organization, User


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    diff_text: str
    pr_title: str
    pr_description: str


class LLMGateway(Protocol):
    async def review(self, request: ReviewRequest) -> ReviewResult: ...


@dataclass(frozen=True, slots=True)
class Token:
    """A user-to-server token. Used once, at sign-in, then discarded.

    repr is suppressed on every secret so a traceback or a log line that
    formats this object cannot leak it.
    """

    access_token: str = field(repr=False)
    token_type: str = "bearer"
    expires_in: int | None = None
    refresh_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class Profile:
    """Who signed in, and which accounts they can reach, already mapped to the domain."""

    user: User
    organizations: tuple[Organization, ...]


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdentityProvider(Protocol):
    def authorize_url(self, state: str, code_challenge: str) -> str: ...

    async def exchange_code(self, code: str, code_verifier: str) -> Token: ...

    async def load_profile(self, token: Token) -> Profile: ...


class SessionStore(Protocol):
    async def create(self, session: Session) -> None: ...

    async def get(self, id_hash: str) -> Session | None: ...

    async def save(self, session: Session) -> None: ...

    async def delete(self, id_hash: str) -> None: ...


class SignInAttemptStore(Protocol):
    async def put(self, attempt: SignInAttempt) -> None: ...

    async def take(self, attempt_id: str) -> SignInAttempt | None:
        """Single use: a replayed attempt id finds nothing."""
        ...


class InstallationGateway(Protocol):
    """Reads what an installation can see on the forge."""

    async def list_repositories(self, installation_id: int) -> tuple[Repository, ...]: ...


class RepositoriesCache(Protocol):
    """A short-lived cache, keyed by installation.

    The repository list is forge state, so it is read live and cached rather than
    stored; `expires_at` is absolute so the cache needs no clock of its own.
    """

    async def get(self, installation_id: int) -> tuple[Repository, ...] | None: ...

    async def put(
        self,
        installation_id: int,
        repositories: tuple[Repository, ...],
        expires_at: datetime,
    ) -> None: ...

    async def drop(self, installation_id: int) -> None: ...


# --- Review pipeline: pull request context and jobs ------------------------


_SHA_RE = re.compile(r"[0-9a-f]{40}")


def _validate_sha(field: str, sha: str) -> None:
    if _SHA_RE.fullmatch(sha) is None:
        raise ValueError(f"{field} must be 40 lowercase hex characters")


@dataclass(frozen=True, slots=True)
class CommitInfo:
    """One commit on a pull request. The message may be empty; git allows it."""

    sha: str
    message: str
    author_login: str | None

    def __post_init__(self) -> None:
        _validate_sha("commit sha", self.sha)


@dataclass(frozen=True, slots=True)
class PullRequestContext:
    """Everything a review needs about one pull request, diff included.

    repr is suppressed on diff_text so a traceback or a log line that formats
    this object cannot leak diff content.
    """

    installation_id: int
    repo_full_name: str
    pr_number: int
    title: str
    description: str
    head_sha: str
    base_sha: str
    head_ref: str
    base_ref: str
    author_login: str | None
    author_external_id: int | None
    author_association: str | None
    commits: tuple[CommitInfo, ...]
    diff_text: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.installation_id < 1:
            raise ValueError(f"installation_id must be >= 1, got {self.installation_id}")
        if self.pr_number < 1:
            raise ValueError(f"pr_number must be >= 1, got {self.pr_number}")
        owner, _, name = self.repo_full_name.partition("/")
        if not owner.strip() or not name.strip() or "/" in name:
            raise ValueError(f"repo_full_name must be 'owner/name', got {self.repo_full_name!r}")
        _validate_sha("head_sha", self.head_sha)
        _validate_sha("base_sha", self.base_sha)


class ReviewJobStatus(StrEnum):
    """The status set of docs/PIPELINE_SPEC.md §2 [D1] and WORKFLOW_DESIGN §7."""

    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    RETRYING = "RETRYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class JobStats:
    """What a review pass looked at, counted per SkipReason name."""

    files_total: int
    files_reviewable: int
    hunks_total: int
    chunks_total: int
    skipped_counts: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        for name, value in (
            ("files_total", self.files_total),
            ("files_reviewable", self.files_reviewable),
            ("hunks_total", self.hunks_total),
            ("chunks_total", self.chunks_total),
        ):
            if value < 0:
                raise ValueError(f"{name} must be >= 0, got {value}")
        if self.files_reviewable > self.files_total:
            raise ValueError(
                f"files_reviewable must not exceed files_total, "
                f"got {self.files_reviewable} > {self.files_total}"
            )
        for reason, count in self.skipped_counts:
            if not reason.strip():
                raise ValueError("skipped_counts reason must not be empty")
            if count < 1:
                raise ValueError(f"skipped_counts count must be >= 1, got {count}")
        reasons = [reason for reason, _ in self.skipped_counts]
        if len(set(reasons)) != len(reasons):
            raise ValueError("skipped_counts reasons must be unique")


@dataclass(frozen=True, slots=True)
class NewReviewJob:
    """A review to run, as first recorded from a webhook delivery.

    The D5 metadata (docs/PIPELINE_SPEC.md §7.2) rides along so the review
    list is readable without a forge round-trip; callers clamp the
    untrusted strings to their VARCHAR(255) columns with
    `domain.jobs.clamp_for_storage` before building this.
    """

    provider: str
    delivery_id: str | None
    installation_id: int
    repo_id: int
    repo_full_name: str
    pr_number: int
    head_sha: str
    base_sha: str
    event_action: str
    pr_title: str
    author_login: str | None = None
    head_ref: str | None = None
    base_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("provider must not be empty")
        if not self.event_action.strip():
            raise ValueError("event_action must not be empty")
        for name, value in (
            ("installation_id", self.installation_id),
            ("repo_id", self.repo_id),
            ("pr_number", self.pr_number),
        ):
            if value < 1:
                raise ValueError(f"{name} must be >= 1, got {value}")
        _validate_sha("head_sha", self.head_sha)
        _validate_sha("base_sha", self.base_sha)


@dataclass(frozen=True, slots=True)
class ReviewJob:
    """A recorded review and where it stands."""

    job_id: str
    created_at: datetime
    status: ReviewJobStatus
    payload: NewReviewJob
    stats: JobStats | None = None
    error_kind: str | None = None
    finished_at: datetime | None = None
    retry_count: int = 0
    next_attempt_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.job_id.strip():
            raise ValueError("job_id must not be empty")


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """A job handed to a worker by `JobRepository.claim`.

    The job is already PROCESSING and leased to `worker_id`;
    `attempt` is the job's retry_count at claim time.
    """

    job_id: str
    payload: NewReviewJob
    attempt: int
    worker_id: str


class GitProvider(Protocol):
    """Reads a pull request's state and diff from the forge."""

    async def fetch_pull_request(
        self, installation_id: int, repo_full_name: str, number: int
    ) -> PullRequestContext: ...


class JobRepository(Protocol):
    """Persists review jobs and their status transitions.

    Who writes which status (docs/WORKFLOW_DESIGN.md §7): only the ingest
    path writes SUPERSEDED; only a worker holding the lease writes
    PROCESSING (via claim), COMPLETED, RETRYING and FAILED. Terminal states
    are never re-entered — a re-run enqueues a new job.

    Contract of the returns: bool methods return False, and `fail` returns
    None, when the job is missing, already terminal, or leased to another
    worker; the caller must stop processing that job.
    """

    async def enqueue(self, job: NewReviewJob) -> str | None:
        """Returns the new job id, or None when the delivery is a duplicate
        (provider, delivery_id). Supersedes the pull request's active jobs
        (QUEUED/RETRYING/PROCESSING) in the same step."""
        ...

    async def claim(self, worker_id: str) -> ClaimedJob | None:
        """Marks the oldest eligible job PROCESSING and leases it to
        `worker_id`: QUEUED/RETRYING with `next_attempt_at <= now`, ordered
        by next_attempt_at (set at enqueue and after each retry), then
        created_at, skipping installations already running
        MAX_IN_FLIGHT_PER_INSTALLATION jobs. Returns None when nothing is
        eligible (docs/WORKFLOW_DESIGN.md §2 Step 4). The column is NOT
        NULL in the schema — PostgreSQL ASC would sort NULLS LAST — so
        both stores see the same order.
        """
        ...

    async def complete(self, job_id: str, stats: JobStats, *, worker_id: str) -> bool:
        """PROCESSING and leased to `worker_id` → COMPLETED with stats and
        finished_at; False otherwise."""
        ...

    async def fail(
        self, job_id: str, error_kind: str, *, retryable: bool, worker_id: str
    ) -> ReviewJobStatus | None:
        """Holder-checked like complete. Retryable with retries left →
        RETRYING, retry_count + 1, next_attempt_at = now + backoff on the
        pre-increment retry_count, 1/2/4 min (docs/PIPELINE_SPEC.md §4.3;
        the lease is cleared); retryable but exhausted, or not retryable →
        FAILED, with retry_count capped at max_retries
        (ck_pr_review_jobs_retry_bounds). Returns the new status, or None
        when not the holder.
        """
        ...

    async def extend_lease(self, job_id: str, *, worker_id: str) -> bool:
        """Holder-checked; pushes locked_until out by LEASE_SECONDS. False
        when the job is not PROCESSING or held by another worker."""
        ...

    async def get(self, job_id: str) -> ReviewJob | None: ...
