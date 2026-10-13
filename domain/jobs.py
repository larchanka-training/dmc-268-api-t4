"""Constants and helpers shared by every review-job store.

Pure domain code (AGENTS.md hard rule 1): no imports from `adapters/`,
`api/`, `worker/` or any framework, and no I/O. The in-memory store and the
future PostgreSQL queue both import this module, so the numbers below live
exactly once and cannot drift between the two implementations.

It also holds the progress events streamed by `GET /jobs/{id}/events`: the step set of
docs/PIPELINE_SPEC.md §2 [D2]. The status set is `domain.ports.ReviewJobStatus`.
"""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from domain.ports import ReviewJobStatus

#: At most this many jobs per installation may be PROCESSING at once —
#: fairness, not capacity (docs/BACKEND_ARCHITECTURE.md § Processes,
#: `max_in_flight_per_account`; docs/WORKFLOW_DESIGN.md §2 Step 4). Keyed on
#: installation_id: an MVP deviation until tenancy lands.
MAX_IN_FLIGHT_PER_INSTALLATION = 3

#: Retries allowed before a retryable failure becomes FAILED
#: (docs/PIPELINE_SPEC.md §4.2, `max_retries` default 3).
DEFAULT_MAX_RETRIES = 3

#: The claim lease: how long a worker may hold a job before the reaper
#: reclaims it — 10 minutes (docs/WORKFLOW_DESIGN.md §2 Step 4;
#: docs/PIPELINE_SPEC.md §3).
LEASE_SECONDS = 600

_BACKOFF_BASE = timedelta(seconds=60)
_BACKOFF_CAP = timedelta(minutes=15)
#: docs/PIPELINE_SPEC.md §4.3: jitter is ±20% of the base delay.
_JITTER_FRACTION = 0.2


def backoff_delay(retry_count: int, rng: random.Random) -> timedelta:
    """The retry delay after `retry_count` recorded retries.

    docs/PIPELINE_SPEC.md §4.3: ``min(60s * 2**retry_count, 15min)`` scaled
    by a uniform jitter factor in [0.8, 1.2], so a provider outage does not
    produce a synchronised retry storm when it ends. Pure: the rng is
    injected, so stores stay testable without sleeping or seeding globals.
    """
    if retry_count < 0:
        raise ValueError(f"retry_count must be >= 0, got {retry_count}")
    base = min(_BACKOFF_BASE * 2**retry_count, _BACKOFF_CAP)
    factor = 1.0 + rng.uniform(-_JITTER_FRACTION, _JITTER_FRACTION)
    return timedelta(seconds=base.total_seconds() * factor)


def clamp_for_storage(value: str, limit: int = 255) -> str:
    """Truncate a string so it fits its VARCHAR(255) column.

    The D5 metadata columns (`pr_title` can exceed the limit; see
    docs/PIPELINE_SPEC.md §7.2) store webhook-supplied strings that were
    never size-checked by the forge. Called by the code building a
    NewReviewJob — the dataclass itself stays validation-free for these
    untrusted values.
    """
    if len(value) <= limit:
        return value
    return value[:limit]


#: Statuses a job never leaves (docs/WORKFLOW_DESIGN.md §7).
TERMINAL_STATUSES = frozenset(
    {
        ReviewJobStatus.COMPLETED,
        ReviewJobStatus.FAILED,
        ReviewJobStatus.SUPERSEDED,
        ReviewJobStatus.SKIPPED,
    }
)


def is_terminal(status: ReviewJobStatus) -> bool:
    return status in TERMINAL_STATUSES


class JobStep(StrEnum):
    # The same set as the ck_pr_review_steps_step constraint (migration 0001).
    CLAIMED = "CLAIMED"
    AUTH = "AUTH"
    FETCH_DIFF = "FETCH_DIFF"
    BUILD_CONTEXT = "BUILD_CONTEXT"
    REDACT = "REDACT"
    GATE = "GATE"
    LLM_CALL = "LLM_CALL"
    POSTPROCESS = "POSTPROCESS"
    POST_FEEDBACK = "POST_FEEDBACK"
    METER = "METER"


class StepStatus(StrEnum):
    RUNNING = "RUNNING"
    OK = "OK"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class UiStage(StrEnum):
    """The stages of the dashboard's progress bar.

    Each event carries both its step and its stage, so the console draws the bar
    without knowing the step set.
    """

    COLLECTING_CONTEXT = "collecting_context"
    REVIEWING = "reviewing"
    VALIDATING = "validating"
    PUBLISHING = "publishing"


_STAGE_OF_STEP = {
    JobStep.CLAIMED: UiStage.COLLECTING_CONTEXT,
    JobStep.AUTH: UiStage.COLLECTING_CONTEXT,
    JobStep.FETCH_DIFF: UiStage.COLLECTING_CONTEXT,
    JobStep.BUILD_CONTEXT: UiStage.COLLECTING_CONTEXT,
    JobStep.REDACT: UiStage.COLLECTING_CONTEXT,
    JobStep.GATE: UiStage.REVIEWING,
    JobStep.LLM_CALL: UiStage.REVIEWING,
    JobStep.POSTPROCESS: UiStage.VALIDATING,
    JobStep.POST_FEEDBACK: UiStage.PUBLISHING,
    # Metering happens after the comments are up; to the user it is still publishing.
    JobStep.METER: UiStage.PUBLISHING,
}


def ui_stage(step: JobStep) -> UiStage:
    return _STAGE_OF_STEP[step]


@dataclass(frozen=True, slots=True)
class JobEvent:
    """One change to a job: its status moved, or a step started or finished.

    `seq` increases per job and is the SSE event id. Shape only: no diff, prompt or model
    text, the same rule as the step rows (WORKFLOW_DESIGN.md, Step tracing).
    """

    seq: int
    job_id: str
    at: datetime
    status: ReviewJobStatus
    step: JobStep | None = None
    step_status: StepStatus | None = None
    attempt: int = 0
    error_kind: str | None = None


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    """A job's state at one moment: what a client draws before following events.

    `account_id` is the GitHub organisation id of the owning account; the stream is
    served only to sessions that belong to it.
    """

    job_id: str
    account_id: str
    status: ReviewJobStatus
    last_seq: int
    steps: tuple[JobEvent, ...] = ()
