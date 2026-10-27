"""Constants and helpers shared by every review-job store.

Pure domain code (AGENTS.md hard rule 1): no imports from `adapters/`,
`api/`, `worker/` or any framework, and no I/O. The in-memory store and the
future PostgreSQL queue both import this module, so the numbers below live
exactly once and cannot drift between the two implementations.

It also holds the progress events streamed by `GET /jobs/{id}/events`: the step set of
docs/PIPELINE_SPEC.md §2 [D2]. The status set is `domain.ports.ReviewJobStatus`.
"""

import base64
import binascii
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from domain.ports import ReviewJob, ReviewJobStatus

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


#: The metric keys a step row may carry (CONTEXT_AND_VERIFICATION_SPEC [C6]).
#: Counters only, plus the single enum-like `context_cut`; the values are
#: validated by `validate_step_metrics` before a row is built.
STEP_METRIC_KEYS = frozenset(
    {
        "context_files",
        "context_fetches",
        "tier1_symbols",
        "tier2_symbols",
        "fallback_files",
        "parse_errors",
        "context_tokens",
        "context_cut",
    }
)

_CONTEXT_CUT_VALUES = ("none", "tier2", "tier1", "fallback")


def validate_step_metrics(metrics: Mapping[str, int | str]) -> None:
    """Allowlist and type-check step metrics, so nothing else rides along into storage."""
    unknown = sorted(set(metrics) - STEP_METRIC_KEYS)
    if unknown:
        raise ValueError(f"unknown step metric keys: {unknown}")
    for key, value in metrics.items():
        if key == "context_cut":
            if value not in _CONTEXT_CUT_VALUES:
                raise ValueError(f"context_cut must be one of {_CONTEXT_CUT_VALUES}, got {value!r}")
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"step metric {key} must be a non-negative integer, got {value!r}")


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

    The stream is served only to sessions with an organisation on `installation_id`.
    """

    job_id: str
    installation_id: int
    status: ReviewJobStatus
    last_seq: int
    steps: tuple[JobEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One row of `pr_review_steps`: a step of one attempt and how far it got.

    `metrics` carries counters only (see STEP_METRIC_KEYS): never rule text,
    source or prompt content (AGENTS.md hard rule 3).
    """

    step: JobStep
    attempt: int
    status: StepStatus
    started_at: datetime
    finished_at: datetime | None = None
    error_kind: str | None = None
    metrics: Mapping[str, int | str] | None = None

    def __post_init__(self) -> None:
        if self.metrics is not None:
            validate_step_metrics(self.metrics)


def events_from_steps(
    job_id: str,
    status: ReviewJobStatus,
    retry_count: int,
    status_at: datetime,
    steps: Sequence[StepRecord],
) -> tuple[JobEvent, ...]:
    """A job's event history, rebuilt from its step rows and its current status.

    Each step gives a RUNNING event when it starts and a second one when it finishes.
    An attempt below `retry_count` is followed by a RETRYING event, and a terminal job
    ends with its status. New rows and transitions only ever append, so `seq` (the
    position in this list) stays stable between reads.
    """
    timed: list[tuple[datetime, int, int, JobEvent]] = []
    for index, record in enumerate(steps):
        base = JobEvent(
            seq=0,
            job_id=job_id,
            at=record.started_at,
            status=ReviewJobStatus.PROCESSING,
            step=record.step,
            step_status=StepStatus.RUNNING,
            attempt=record.attempt,
        )
        timed.append((record.started_at, index, 0, base))
        if record.finished_at is not None:
            finished = JobEvent(
                seq=0,
                job_id=job_id,
                at=record.finished_at,
                status=ReviewJobStatus.PROCESSING,
                step=record.step,
                step_status=record.status,
                attempt=record.attempt,
                error_kind=record.error_kind,
            )
            timed.append((record.finished_at, index, 1, finished))

    events: list[JobEvent] = []
    attempts = sorted({record.attempt for record in steps})
    for attempt in attempts:
        in_attempt = sorted(
            (entry for entry in timed if entry[3].attempt == attempt), key=lambda e: e[:3]
        )
        events.extend(entry[3] for entry in in_attempt)
        if attempt < retry_count:
            events.append(
                JobEvent(
                    seq=0,
                    job_id=job_id,
                    at=events[-1].at,
                    status=ReviewJobStatus.RETRYING,
                    attempt=attempt,
                )
            )
    if is_terminal(status):
        events.append(
            JobEvent(seq=0, job_id=job_id, at=status_at, status=status, attempt=retry_count)
        )
    return tuple(replace(event, seq=seq) for seq, event in enumerate(events, start=1))


def latest_steps(events: Sequence[JobEvent]) -> tuple[JobEvent, ...]:
    """The latest event of each (step, attempt), in the order the steps started."""
    latest: dict[tuple[JobStep, int], JobEvent] = {}
    for event in events:
        if event.step is not None:
            latest[(event.step, event.attempt)] = event
    return tuple(latest.values())


#: `GET /jobs?status=active`: the jobs still in flight.
ACTIVE_STATUSES = frozenset(
    {ReviewJobStatus.QUEUED, ReviewJobStatus.PROCESSING, ReviewJobStatus.RETRYING}
)


@dataclass(frozen=True, slots=True)
class JobQuery:
    """One page of `GET /jobs`, newest first.

    Empty `statuses` means every status. `after` is the (created_at, job_id) of the last
    job on the previous page. `text` matches the pull request title, or its number when
    it is one.
    """

    installation_ids: frozenset[int]
    statuses: frozenset[ReviewJobStatus] = frozenset()
    repository: str | None = None
    author: str | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    text: str | None = None
    after: tuple[datetime, str] | None = None
    limit: int = 25


@dataclass(frozen=True, slots=True)
class JobPage:
    items: tuple[ReviewJob, ...]
    # The position after the last item, or None when the list ends.
    next_after: tuple[datetime, str] | None


def encode_cursor(position: tuple[datetime, str]) -> str:
    created_at, job_id = position
    raw = f"{created_at.isoformat()}|{job_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Raises ValueError for anything `encode_cursor` did not produce."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError):
        raise ValueError("bad cursor") from None
    created_at, separator, job_id = raw.partition("|")
    if not separator or not job_id:
        raise ValueError("bad cursor")
    parsed = datetime.fromisoformat(created_at)
    if parsed.tzinfo is None:
        raise ValueError("bad cursor")
    return parsed, job_id
