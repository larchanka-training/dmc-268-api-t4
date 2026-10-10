"""Run one review job: fetch, parse, filter, record stats.

This is Step 5 of the review workflow (docs/WORKFLOW_DESIGN.md §2): the
webhook enqueues a job and starts this coroutine as a background task
(tasks/plan.md decision 2, 2026-10-07). The worker claim loop (§2 Step 4)
replaces the direct invocation later; the function signature stays.

Logs carry job id, provider, repo, PR number, file paths with skip reasons,
counts, error kind and duration — never PR title or description, and never
diff text. The single, explicit exception is the dev-only DEBUG_LOG_DIFF=1
escape hatch (see _debug_log_diff_enabled), which exists so a developer can
inspect what a real delivery fetched; it must never be set outside a
developer machine.
"""

import logging
import os
import time

from domain.diff import parse_diff, split_into_chunks
from domain.diff_filter import filter_files
from domain.errors import DiffFormatError, ForgeError
from domain.ports import GitProvider, JobRepository, JobStats, NewReviewJob

#: Dev-only escape hatch: when "1", the fetched diff text is logged verbatim
#: after the fetch. This knowingly deviates from AGENTS.md hard rule 3 (raw
#: diffs are never logged) and is the only place in the codebase that does.
DEBUG_LOG_DIFF_ENV = "DEBUG_LOG_DIFF"


def _debug_log_diff_enabled() -> bool:
    # Read per call, not at import, so tests and operators can flip it.
    return os.environ.get(DEBUG_LOG_DIFF_ENV, "") == "1"


def _clip(value: str, limit: int = 2000) -> str:
    """Cap a joined inventory string so an absurd diff cannot make an absurd
    log line; paths and reasons are metadata, the volume is not."""
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


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
        if _debug_log_diff_enabled():
            # Logged before parsing on purpose: a diff that fails to parse is
            # exactly the case a developer needs to see.
            log.info(
                "review job diff (%s=1): job_id=%s repo=%s pr=%s diff_text=%s",
                DEBUG_LOG_DIFF_ENV,
                job_id,
                payload.repo_full_name,
                payload.pr_number,
                context.diff_text,
            )
        files = parse_diff(context.diff_text)
        filtered = filter_files(files)
        kept_paths = ", ".join(diff.new_path or diff.old_path for diff in filtered.kept)
        skipped_paths = ", ".join(
            f"{entry.path} ({entry.reason.value})" for entry in filtered.skipped
        )
        log.info(
            "review job files: job_id=%s repo=%s pr=%s reviewable=[%s] skipped=[%s]",
            job_id,
            payload.repo_full_name,
            payload.pr_number,
            _clip(kept_paths),
            _clip(skipped_paths),
        )
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
