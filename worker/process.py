"""Run one claimed review job: fetch, parse, filter, record stats.

This is Step 5 of the review workflow (docs/WORKFLOW_DESIGN.md §2). The
production caller is the worker claim loop (§2 Step 4); the webhook runs the
same function inline as a one-shot claimer until that loop lands
(tasks/plan.md decision 2, 2026-10-07).

The job arrives already claimed — PROCESSING and leased to
`claimed.worker_id` — so this stage never claims by itself. It does not call
`extend_lease` either: this stage is seconds (one forge round-trip and
parsing), not minutes; lease extension starts with the LLM stage
(docs/WORKFLOW_DESIGN.md §6).

Logs carry job id, provider, repo, PR number, file paths with skip reasons,
counts, error kind, retry attempt and next attempt time — never PR title or
description, and never diff text. The single, explicit exception is the
dev-only DEBUG_LOG_DIFF=1 escape hatch (see _debug_log_diff_enabled), which
exists so a developer can inspect what a real delivery fetched; it must
never be set outside a developer machine.
"""

import logging
import os
import time
from collections.abc import Awaitable
from dataclasses import dataclass, replace
from datetime import datetime

from adapters.observability.config import DEFAULT_ENV, read_env
from domain.diff import parse_diff, split_into_chunks
from domain.diff_filter import filter_files
from domain.errors import (
    AllProvidersFailedError,
    DiffFormatError,
    ForgeUnavailableError,
    InstallationGoneError,
    LLMInputTooLargeError,
    LLMOutputError,
    PrNotFoundError,
    ProviderUnavailableError,
)
from domain.findings import NewPublishedFinding, published_findings
from domain.jobs import JobStep, StepStatus
from domain.ports import (
    ClaimedJob,
    GitProvider,
    JobRepository,
    JobStats,
    LLMGateway,
    PullRequestTarget,
    ReviewCommentStore,
    ReviewJobStatus,
    ReviewPublisher,
    ReviewRequest,
    StepRecorder,
)
from domain.publishing import DEFAULT_POLICY, CommentableLines, PublishPolicy, build_plan


@dataclass(frozen=True, slots=True)
class ReviewStage:
    """What turns a parsed diff into a posted review: the LLM, then the forge.

    Optional: without it the job stops after fetching and parsing the diff (tests and
    the in-memory dev mode)."""

    gateway: LLMGateway
    publisher: ReviewPublisher
    policy: PublishPolicy = DEFAULT_POLICY
    # Where published findings are kept, and what tells a re-review which ones the
    # pull request already has. Without it every review posts every finding again.
    comments: ReviewCommentStore | None = None


#: Dev-only escape hatch: when "1", the fetched diff text is logged verbatim
#: after the fetch. This knowingly deviates from AGENTS.md hard rule 3 (raw
#: diffs are never logged) and is the only place in the codebase that does,
#: so it works only in ENV=local (docs/observability.md § What is never
#: logged); the log allowlist also drops ``diff_text`` everywhere else.
DEBUG_LOG_DIFF_ENV = "DEBUG_LOG_DIFF"


def _debug_log_diff_enabled() -> bool:
    # Read per call, not at import, so tests and operators can flip it.
    return os.environ.get(DEBUG_LOG_DIFF_ENV, "") == "1" and read_env(os.environ) == DEFAULT_ENV


def _clip(value: str, limit: int = 2000) -> str:
    """Cap a joined inventory string so an absurd diff cannot make an absurd
    log line; paths and reasons are metadata, the volume is not."""
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


class _Steps:
    """The step the job is in, mirrored to a `StepRecorder` when one is given.

    Recording is progress for the console, never part of the job's outcome: a failed
    write is logged (exception class only) and the job goes on."""

    def __init__(
        self, recorder: StepRecorder | None, claimed: ClaimedJob, log: logging.Logger
    ) -> None:
        self._recorder = recorder
        self._claimed = claimed
        self._log = log
        self._current: JobStep | None = None

    async def begin(self, step: JobStep) -> None:
        """Finish the current step as OK and start `step`."""
        await self.end(StepStatus.OK)
        self._current = step
        if self._recorder is not None:
            await self._guarded(
                self._recorder.start(
                    self._claimed.job_id,
                    self._claimed.attempt,
                    step,
                    worker_id=self._claimed.worker_id,
                )
            )

    async def end(self, status: StepStatus, error_kind: str | None = None) -> None:
        step, self._current = self._current, None
        if step is None or self._recorder is None:
            return
        await self._guarded(
            self._recorder.finish(
                self._claimed.job_id,
                self._claimed.attempt,
                step,
                status,
                worker_id=self._claimed.worker_id,
                error_kind=error_kind,
            )
        )

    async def _guarded(self, write: Awaitable[None]) -> None:
        try:
            await write
        except Exception as err:
            self._log.error(
                "job.step_record.failed",
                extra={"job_id": self._claimed.job_id, "error_type": type(err).__name__},
            )


def _classify(error: Exception) -> tuple[str, bool]:
    """Map an exception to (error_kind, retryable) — the closed error
    vocabulary of docs/PIPELINE_SPEC.md §4.2/§5."""
    if isinstance(error, ForgeUnavailableError):
        return "forge_unavailable", True
    if isinstance(error, InstallationGoneError):
        return "installation_removed", False
    if isinstance(error, PrNotFoundError):
        return "pr_not_found", False
    if isinstance(error, DiffFormatError):
        return "diff_parse_failed", False
    if isinstance(error, (AllProvidersFailedError, ProviderUnavailableError)):
        return "provider_unavailable", True
    if isinstance(error, LLMOutputError):
        return "llm_output_invalid", False
    if isinstance(error, LLMInputTooLargeError):
        return "skip_diff_too_large", False
    return "internal_error", False


async def process_claimed_job(
    claimed: ClaimedJob,
    vcs: GitProvider,
    jobs: JobRepository,
    *,
    review: ReviewStage | None = None,
    steps: StepRecorder | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    """Fetch and parse one claimed pull request diff, review and publish it when a
    review stage is given; record stats or failure, and each step in `steps`.

    Returns True only when the job reached COMPLETED. Returns False when the
    lease was lost mid-flight (a superseding delivery or the reaper owns the
    job now), when the run failed terminally (FAILED), or when a retryable
    failure sent it to RETRYING. Unexpected errors — including the store
    raising on a terminal transition — are swallowed: a claimed job has no
    caller to raise to; only the exception class is logged.
    """
    log = logger if logger is not None else logging.getLogger(__name__)
    started = time.monotonic()
    job_id = claimed.job_id
    payload = claimed.payload
    progress = _Steps(steps, claimed, log)

    try:
        await progress.begin(JobStep.CLAIMED)
        await progress.begin(JobStep.FETCH_DIFF)
        context = await vcs.fetch_pull_request(
            payload.installation_id, payload.repo_full_name, payload.pr_number
        )
        if _debug_log_diff_enabled():
            # Logged before parsing on purpose: a diff that fails to parse is
            # exactly the case a developer needs to see.
            log.info(
                "job.diff.debug",
                extra={
                    "job_id": job_id,
                    "repo": payload.repo_full_name,
                    "pr_number": payload.pr_number,
                    "diff_text": context.diff_text,
                },
            )
        files = parse_diff(context.diff_text)
        filtered = filter_files(files)
        kept_paths = ", ".join(diff.new_path or diff.old_path for diff in filtered.kept)
        skipped_paths = ", ".join(
            f"{entry.path} ({entry.reason.value})" for entry in filtered.skipped
        )
        log.info(
            "job.files",
            extra={
                "job_id": job_id,
                "repo": payload.repo_full_name,
                "pr_number": payload.pr_number,
                "reviewable_paths": _clip(kept_paths),
                "skipped_paths": _clip(skipped_paths),
            },
        )
        stats = JobStats(
            files_total=len(files),
            files_reviewable=len(filtered.kept),
            hunks_total=sum(len(diff.hunks) for diff in files),
            chunks_total=sum(len(split_into_chunks(diff)) for diff in filtered.kept),
            skipped_counts=tuple((reason.value, count) for reason, count in filtered.skip_counts),
        )
        if review is not None:
            # LLM_CALL can take minutes (two 300 s attempts plus a fallback): renew the
            # lease before it and again before posting, and stop if it was lost.
            if not await jobs.extend_lease(job_id, worker_id=claimed.worker_id):
                return False
            await progress.begin(JobStep.LLM_CALL)
            result = await review.gateway.review(
                ReviewRequest(
                    diff_text=context.diff_text,
                    pr_title=context.title,
                    pr_description=context.description,
                )
            )
            posted = (
                frozenset()
                if review.comments is None
                else await review.comments.posted_fingerprints(
                    provider=payload.provider,
                    installation_id=payload.installation_id,
                    repo_id=payload.repo_id,
                    pr_number=payload.pr_number,
                )
            )
            plan = build_plan(
                result.findings,
                model_summary=result.summary,
                lines=CommentableLines.from_files(filtered.kept),
                policy=review.policy,
                posted_fingerprints=posted,
            )
            if not await jobs.extend_lease(job_id, worker_id=claimed.worker_id):
                return False
            await progress.begin(JobStep.POST_FEEDBACK)
            owner, repo = payload.repo_full_name.split("/", 1)
            outcome = await review.publisher.publish(
                PullRequestTarget(
                    installation_id=payload.installation_id,
                    owner=owner,
                    repo=repo,
                    number=payload.pr_number,
                    head_sha=context.head_sha,
                ),
                plan,
            )
            stats = replace(stats, findings_published=len(outcome.comments))
            if review.comments is not None:
                await _keep_findings(
                    review.comments, claimed, published_findings(plan, outcome), log
                )
        await progress.end(StepStatus.OK)
    except Exception as err:
        error_kind, retryable = _classify(err)
        await progress.end(StepStatus.FAILED, error_kind)
        return await _record_failure(
            jobs,
            claimed,
            log,
            started,
            error_kind=error_kind,
            retryable=retryable,
            exc_class=type(err).__name__,
        )

    try:
        completed = await jobs.complete(job_id, stats, worker_id=claimed.worker_id)
    except Exception as err:
        log.error(
            "job.store.failed",
            extra={
                **_job_fields(claimed),
                "stage": "complete",
                "error_type": type(err).__name__,
                "duration_ms": _elapsed_ms(started),
            },
        )
        return False
    if not completed:
        return False
    log.info(
        "job.finished",
        extra={
            **_job_fields(claimed),
            "outcome": ReviewJobStatus.COMPLETED.value,
            "files": stats.files_total,
            "reviewable": stats.files_reviewable,
            "hunks": stats.hunks_total,
            "chunks": stats.chunks_total,
            "duration_ms": _elapsed_ms(started),
        },
    )
    return True


def _job_fields(claimed: ClaimedJob) -> dict[str, object]:
    """The fields every job event carries."""
    payload = claimed.payload
    return {
        "job_id": claimed.job_id,
        "provider": payload.provider,
        "repo": payload.repo_full_name,
        "pr_number": payload.pr_number,
    }


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


async def _keep_findings(
    comments: ReviewCommentStore,
    claimed: ClaimedJob,
    findings: tuple[NewPublishedFinding, ...],
    log: logging.Logger,
) -> None:
    """Record what was posted. A failure here does not fail the job: the review is on
    the forge already, and a retry would post it a second time. The dashboard misses
    these findings and the next review may repeat them; the log says so."""
    try:
        await comments.record(claimed.job_id, findings)
    except Exception as err:
        log.error(
            "job.findings.not_kept",
            extra={
                **_job_fields(claimed),
                "findings": len(findings),
                "error_type": type(err).__name__,
            },
        )


async def _record_failure(
    jobs: JobRepository,
    claimed: ClaimedJob,
    log: logging.Logger,
    started: float,
    *,
    error_kind: str,
    retryable: bool,
    exc_class: str,
) -> bool:
    """Record RETRYING or FAILED with the store, then log which one it was.

    The exception class is logged — never its message, which is safe by
    construction today but is not guaranteed to stay that way. The store
    calls are guarded: raising while handling a failure would lose the
    log-and-stop contract, so a store error is logged (exc_class only) and
    the function still returns False. A None status means the lease was
    lost; nothing is logged because the job's new owner logs its own run."""
    try:
        status = await jobs.fail(
            claimed.job_id,
            error_kind,
            retryable=retryable,
            worker_id=claimed.worker_id,
        )
    except Exception as err:
        log.error(
            "job.store.failed",
            extra={
                **_job_fields(claimed),
                "stage": "fail",
                "error_kind": error_kind,
                "error_type": type(err).__name__,
                "duration_ms": _elapsed_ms(started),
            },
        )
        return False
    if status is ReviewJobStatus.RETRYING:
        await _log_retrying(jobs, claimed, log, started, error_kind=error_kind)
        return False
    if status is ReviewJobStatus.FAILED:
        log.warning(
            "job.finished",
            extra={
                **_job_fields(claimed),
                "outcome": ReviewJobStatus.FAILED.value,
                "error_kind": error_kind,
                "error_type": exc_class,
                "duration_ms": _elapsed_ms(started),
            },
        )
    return False


async def _log_retrying(
    jobs: JobRepository,
    claimed: ClaimedJob,
    log: logging.Logger,
    started: float,
    *,
    error_kind: str,
) -> None:
    """Log the retry schedule; next_attempt_at is read back from the store,
    which computed it with its own clock and rng. A supersede between the
    fail and this read-back leaves the job with no next_attempt_at; it is
    logged as '-'. The read is guarded like every other store call."""
    next_attempt_at: datetime | None = None
    try:
        stored = await jobs.get(claimed.job_id)
        if stored is not None:
            next_attempt_at = stored.next_attempt_at
    except Exception as err:
        log.error(
            "job.store.failed",
            extra={
                **_job_fields(claimed),
                "stage": "get",
                "error_kind": error_kind,
                "error_type": type(err).__name__,
                "duration_ms": _elapsed_ms(started),
            },
        )
    log.info(
        "job.retrying",
        extra={
            **_job_fields(claimed),
            "error_kind": error_kind,
            "attempt": claimed.attempt + 1,
            "next_attempt_at": next_attempt_at.isoformat() if next_attempt_at else None,
            "duration_ms": _elapsed_ms(started),
        },
    )
