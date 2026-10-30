"""Run one claimed review job: fetch, parse, filter, record stats.

This is Step 5 of the review workflow (docs/WORKFLOW_DESIGN.md §2). The
production caller is the worker claim loop (§2 Step 4); the webhook runs the
same function inline as a one-shot claimer until that loop lands
(docs/tasks/plan.md decision 2, 2026-10-07).

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
from dataclasses import dataclass, replace
from datetime import datetime

from adapters.observability import metrics
from adapters.observability.config import DEFAULT_ENV, read_env
from adapters.observability.logging import bind_context
from adapters.observability.observer import StepObserver
from adapters.observability.sentry import job_transaction
from domain.diff import parse_diff, split_into_chunks
from domain.diff_filter import filter_files
from domain.errors import LLMInputTooLargeError, classify
from domain.findings import NewPublishedFinding, published_findings
from domain.jobs import JobStep
from domain.ports import (
    ClaimedJob,
    GitProvider,
    JobRepository,
    JobStats,
    LLMGateway,
    Observer,
    PullRequestTarget,
    ReviewCommentStore,
    ReviewJobStatus,
    ReviewPublisher,
    ReviewRequest,
    StepRecorder,
)
from domain.publishing import (
    DEFAULT_POLICY,
    CommentableLines,
    PublishPolicy,
    build_plan,
    build_skipped_plan,
)


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


#: The steps a run records today (docs/observability.md § Review pipeline); the other
#: JobStep values join as they are built. Their duration series start at 0.
RECORDED_STEPS = (
    JobStep.CLAIMED,
    JobStep.FETCH_DIFF,
    JobStep.BUILD_CONTEXT,
    JobStep.LLM_CALL,
    JobStep.POSTPROCESS,
    JobStep.POST_FEEDBACK,
)

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


async def process_claimed_job(
    claimed: ClaimedJob,
    vcs: GitProvider,
    jobs: JobRepository,
    *,
    review: ReviewStage | None = None,
    steps: StepRecorder | None = None,
    logger: logging.Logger | None = None,
    observer: Observer | None = None,
) -> bool:
    """Fetch and parse one claimed pull request diff, review and publish it when a
    review stage is given; record stats or failure, and each step in `steps`.

    Each step goes through an `Observer` (its row, span, duration and log line); by
    default the `StepObserver` over `steps`. The run is one ``review.job`` transaction
    that continues the webhook's trace, so the webhook and the review are one trace.

    Returns True only when the job reached COMPLETED. Returns False when the
    lease was lost mid-flight (a superseding delivery or the reaper owns the
    job now), when the run failed terminally (FAILED), or when a retryable
    failure sent it to RETRYING. Unexpected errors — including the store
    raising on a terminal transition — are swallowed: a claimed job has no
    caller to raise to. A job that ends FAILED is logged at error with its
    exception, which is what makes it one Sentry event; the log line itself
    carries the exception's type and frames, never its message.

    Every line logged and every Sentry event raised during the run carries the
    job's context (docs/observability.md § Fields), and the run is counted in the
    worker's job metrics (§ Metrics).
    """
    if claimed.claimable_at is not None and claimed.claimed_at is not None:
        metrics.observe_claim_latency((claimed.claimed_at - claimed.claimable_at).total_seconds())
    log = logger if logger is not None else logging.getLogger(__name__)
    with (
        bind_context(**_bound_fields(claimed)),
        metrics.WORKER_IN_FLIGHT.track_inprogress(),
        job_transaction(claimed.payload.trace_context),
    ):
        return await _process(
            claimed,
            vcs,
            jobs,
            review=review,
            observer=observer if observer is not None else StepObserver(steps, claimed, log),
            log=log,
        )


def _bound_fields(claimed: ClaimedJob) -> dict[str, object]:
    payload = claimed.payload
    return {
        "job_id": claimed.job_id,
        "installation_id": payload.installation_id,
        "repo_id": payload.repo_id,
        "pr_number": payload.pr_number,
        "head_sha": payload.head_sha,
        "attempt": claimed.attempt,
    }


async def _process(
    claimed: ClaimedJob,
    vcs: GitProvider,
    jobs: JobRepository,
    *,
    review: ReviewStage | None,
    observer: Observer,
    log: logging.Logger,
) -> bool:
    started = time.monotonic()
    job_id = claimed.job_id
    payload = claimed.payload

    try:
        async with observer.step(JobStep.CLAIMED) as step:
            if claimed.enqueued_at is not None and claimed.claimed_at is not None:
                waited = claimed.claimed_at - claimed.enqueued_at
                step.set(queue_wait_ms=round(waited.total_seconds() * 1000))
        async with observer.step(JobStep.FETCH_DIFF) as step:
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
            step.set(bytes=len(context.diff_text.encode()))
        # Parsing and filtering: a diff that does not parse fails here, not in the fetch.
        async with observer.step(JobStep.BUILD_CONTEXT) as step:
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
                skipped_counts=tuple(
                    (reason.value, count) for reason, count in filtered.skip_counts
                ),
            )
            step.set(
                files=stats.files_total,
                reviewable=stats.files_reviewable,
                skipped=len(filtered.skipped),
                chunks=stats.chunks_total,
            )
        summary: str | None = None
        if review is not None:
            # LLM_CALL can take minutes (two 300 s attempts plus a fallback): renew the
            # lease before it and again before posting, and stop if it was lost.
            if not await _extend_lease(jobs, claimed):
                return False
            target = _pull_request_target(claimed, context.head_sha)
            async with observer.step(JobStep.LLM_CALL) as step:
                try:
                    result = await review.gateway.review(
                        ReviewRequest(
                            diff_text=context.diff_text,
                            pr_title=context.title,
                            pr_description=context.description,
                        )
                    )
                except LLMInputTooLargeError as too_large:
                    # The job still fails as skip_diff_too_large below; the check run
                    # only tells the author why nothing else appears.
                    await _publish_skipped(
                        review.publisher, target, too_large, len(filtered.kept), claimed, log
                    )
                    raise
                step.set(
                    provider=result.provider, model=result.model, findings=len(result.findings)
                )
            async with observer.step(JobStep.POSTPROCESS) as step:
                posted: frozenset[str] = frozenset()
                if review.comments is not None:
                    with metrics.timed_db("posted_fingerprints"):
                        posted = await review.comments.posted_fingerprints(
                            provider=payload.provider,
                            installation_id=payload.installation_id,
                            repo_id=payload.repo_id,
                            pr_number=payload.pr_number,
                        )
                plan = build_plan(
                    result.findings,
                    model_summary=result.summary,
                    lines=CommentableLines.from_files(filtered.kept),
                    policy=review.policy,
                    posted_fingerprints=posted,
                    model_risk=result.risk,
                    checklist=result.checklist,
                    dismissed_by_verifier=result.rejected_by_verifier,
                )
                step.set(findings_total=len(result.findings), already_posted=len(posted))
            if not await _extend_lease(jobs, claimed):
                return False
            async with observer.step(JobStep.POST_FEEDBACK) as step:
                outcome = await review.publisher.publish(target, plan)
                step.set(
                    comments_posted=len(outcome.comments), inline_fallback=outcome.inline_fallback
                )
                stats = replace(stats, findings_published=len(outcome.comments))
                summary = plan.plain_summary
                published = published_findings(plan, outcome)
                metrics.count_findings_posted(p.finding.severity.value for p in published)
                if review.comments is not None:
                    await _keep_findings(review.comments, claimed, published, log)
    except Exception as err:
        error_kind, retryable = classify(err)
        return await _record_failure(
            jobs,
            claimed,
            log,
            started,
            error_kind=error_kind,
            retryable=retryable,
            error=err,
        )

    try:
        with metrics.timed_db("complete"):
            completed = await jobs.complete(
                job_id, stats, worker_id=claimed.worker_id, summary=summary
            )
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
    metrics.record_job(
        ReviewJobStatus.COMPLETED, None, review_seconds=_review_seconds(claimed, started)
    )
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


def _review_seconds(claimed: ClaimedJob, started: float) -> float | None:
    """Enqueue to now: the wait before this claim in the store's clock, then this
    run's own time, so two clocks are never compared."""
    if claimed.enqueued_at is None or claimed.claimed_at is None:
        return None
    waited = (claimed.claimed_at - claimed.enqueued_at).total_seconds()
    return waited + time.monotonic() - started


def _pull_request_target(claimed: ClaimedJob, head_sha: str) -> PullRequestTarget:
    payload = claimed.payload
    owner, repo = payload.repo_full_name.split("/", 1)
    return PullRequestTarget(
        installation_id=payload.installation_id,
        owner=owner,
        repo=repo,
        number=payload.pr_number,
        head_sha=head_sha,
    )


async def _publish_skipped(
    publisher: ReviewPublisher,
    target: PullRequestTarget,
    too_large: LLMInputTooLargeError,
    reviewable_files: int,
    claimed: ClaimedJob,
    log: logging.Logger,
) -> None:
    """Post the "skipped" check run, updating the one an earlier run left on this SHA.

    Best effort: a failure is logged (exception class only) and the job is recorded as it
    would be without the check run. The LLM is never called again for this job.
    """
    plan = build_skipped_plan(
        estimated_tokens=too_large.estimated_tokens,
        limit=too_large.limit,
        reviewable_files=reviewable_files,
    )
    fields: dict[str, object] = {
        **_job_fields(claimed),
        "input_tokens": too_large.estimated_tokens,
        "max_input_tokens": too_large.limit,
    }
    try:
        existing = await publisher.find_check_run(target)
        await publisher.publish(target, plan, existing)
    except Exception as err:
        log.warning(
            "job.skipped_check_run",
            extra={**fields, "outcome": "failed", "error_type": type(err).__name__},
        )
        return
    log.info("job.skipped_check_run", extra={**fields, "outcome": "published"})


async def _extend_lease(jobs: JobRepository, claimed: ClaimedJob) -> bool:
    with metrics.timed_db("extend_lease"):
        return await jobs.extend_lease(claimed.job_id, worker_id=claimed.worker_id)


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
        with metrics.timed_db("record_findings"):
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
    error: Exception,
) -> bool:
    """Record RETRYING or FAILED with the store, then log which one it was.

    FAILED is logged at error with the exception: one Sentry event per failed
    job, its message replaced by the error_kind in production
    (adapters/observability/sentry.py), and in the log line only the type and
    frames. RETRYING stays at info and is never an event. The store calls are
    guarded: raising while handling a failure would lose the log-and-stop
    contract, so a store error is logged (error_type only) and the function
    still returns False. A None status means the lease was lost; nothing is
    logged because the job's new owner logs its own run."""
    try:
        with metrics.timed_db("fail"):
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
        metrics.record_job(status, error_kind, review_seconds=None)
        await _log_retrying(jobs, claimed, log, started, error_kind=error_kind)
        return False
    if status is ReviewJobStatus.FAILED:
        metrics.record_job(status, error_kind, review_seconds=_review_seconds(claimed, started))
        log.error(
            "job.finished",
            exc_info=error,
            extra={
                **_job_fields(claimed),
                "outcome": ReviewJobStatus.FAILED.value,
                "error_kind": error_kind,
                "error_type": type(error).__name__,
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
        with metrics.timed_db("get"):
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
