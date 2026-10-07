"""Run one review job: fetch, parse, filter, record stats.

This is Step 5 of the review workflow (docs/WORKFLOW_DESIGN.md §2): the
webhook enqueues a job and starts this coroutine as a background task
(tasks/plan.md decision 2, 2026-10-07). The worker claim loop (§2 Step 4)
replaces the direct invocation later; the function signature stays.

Logs carry job id, provider, repo, PR number, counts, error kind and duration
only — never diff text, PR title or description (AGENTS.md hard rule 3).
"""

import logging
import time

from domain.diff import parse_diff, split_into_chunks
from domain.diff_filter import filter_files
from domain.errors import DiffFormatError, ForgeError
from domain.ports import GitProvider, JobRepository, JobStats, NewReviewJob


async def process_review_job(
    job_id: str,
    payload: NewReviewJob,
    vcs: GitProvider,
    jobs: JobRepository,
    *,
    logger: logging.Logger | None = None,
) -> bool:
    """Fetch and parse one pull request diff; record stats or the failure.

    Returns True only when the job reached COMPLETED. Returns False when the
    job was superseded before start (the forge is never called) or mid-flight
    (it is left to the superseding delivery), or when the run failed and the
    job was marked FAILED. Unexpected errors — including the store raising on
    a terminal transition — are swallowed: a background task has no caller to
    raise to.
    """
    log = logger if logger is not None else logging.getLogger(__name__)
    started = time.monotonic()

    if not await jobs.mark_processing(job_id):
        return False

    try:
        context = await vcs.fetch_pull_request(
            payload.installation_id, payload.repo_full_name, payload.pr_number
        )
        files = parse_diff(context.diff_text)
        filtered = filter_files(files)
        stats = JobStats(
            files_total=len(files),
            files_reviewable=len(filtered.kept),
            hunks_total=sum(len(diff.hunks) for diff in files),
            chunks_total=sum(len(split_into_chunks(diff)) for diff in filtered.kept),
            skipped_counts=tuple((reason.value, count) for reason, count in filtered.skip_counts),
        )
    except ForgeError as err:
        return await _mark_failed(
            jobs,
            job_id,
            payload,
            log,
            started,
            error_kind=type(err).__name__,
            exc_class=type(err).__name__,
        )
    except DiffFormatError as err:
        return await _mark_failed(
            jobs,
            job_id,
            payload,
            log,
            started,
            error_kind="diff_parse_failed",
            exc_class=type(err).__name__,
        )
    except Exception as err:
        return await _mark_failed(
            jobs,
            job_id,
            payload,
            log,
            started,
            error_kind="internal",
            exc_class=type(err).__name__,
        )

    try:
        completed = await jobs.mark_completed(job_id, stats)
    except Exception as err:
        log.error(
            "review job store error on completion: job_id=%s provider=%s repo=%s "
            "pr_number=%s exc_class=%s duration_ms=%.0f",
            job_id,
            payload.provider,
            payload.repo_full_name,
            payload.pr_number,
            type(err).__name__,
            (time.monotonic() - started) * 1000,
        )
        return False
    if not completed:
        return False
    log.info(
        "review job completed: job_id=%s provider=%s repo=%s pr_number=%s "
        "files=%d reviewable=%d hunks=%d chunks=%d duration_ms=%.0f",
        job_id,
        payload.provider,
        payload.repo_full_name,
        payload.pr_number,
        stats.files_total,
        stats.files_reviewable,
        stats.hunks_total,
        stats.chunks_total,
        (time.monotonic() - started) * 1000,
    )
    return True


async def _mark_failed(
    jobs: JobRepository,
    job_id: str,
    payload: NewReviewJob,
    log: logging.Logger,
    started: float,
    *,
    error_kind: str,
    exc_class: str,
) -> bool:
    """Record FAILED and log error_kind and the exception class — not its message,
    which is safe by construction today but is not guaranteed to stay that way.
    The store call itself is guarded: raising while handling a failure would
    lose the log-and-stop contract, so a store error is logged as internal
    (exc_class only) and the function still returns False."""
    log.warning(
        "review job failed: job_id=%s provider=%s repo=%s pr_number=%s "
        "error_kind=%s exc_class=%s duration_ms=%.0f",
        job_id,
        payload.provider,
        payload.repo_full_name,
        payload.pr_number,
        error_kind,
        exc_class,
        (time.monotonic() - started) * 1000,
    )
    try:
        await jobs.mark_failed(job_id, error_kind)
    except Exception as err:
        log.error(
            "review job store error on failure record: job_id=%s provider=%s repo=%s "
            "pr_number=%s error_kind=%s exc_class=%s duration_ms=%.0f",
            job_id,
            payload.provider,
            payload.repo_full_name,
            payload.pr_number,
            error_kind,
            type(err).__name__,
            (time.monotonic() - started) * 1000,
        )
    return False
