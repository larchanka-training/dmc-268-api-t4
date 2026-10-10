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
from datetime import datetime

from domain.diff import parse_diff, split_into_chunks
from domain.diff_filter import filter_files
from domain.errors import (
    DiffFormatError,
    ForgeUnavailableError,
    InstallationGoneError,
    PrNotFoundError,
)
from domain.ports import (
    ClaimedJob,
    GitProvider,
    JobRepository,
    JobStats,
    ReviewJobStatus,
)

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
    return "internal_error", False


async def process_claimed_job(
    claimed: ClaimedJob,
    vcs: GitProvider,
    jobs: JobRepository,
    *,
    logger: logging.Logger | None = None,
) -> bool:
    """Fetch and parse one claimed pull request diff; record stats or failure.

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
    except Exception as err:
        error_kind, retryable = _classify(err)
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
    payload = claimed.payload
    try:
        status = await jobs.fail(
            claimed.job_id,
            error_kind,
            retryable=retryable,
            worker_id=claimed.worker_id,
        )
    except Exception as err:
        log.error(
            "review job store error on failure record: job_id=%s provider=%s repo=%s "
            "pr_number=%s error_kind=%s exc_class=%s duration_ms=%.0f",
            claimed.job_id,
            payload.provider,
            payload.repo_full_name,
            payload.pr_number,
            error_kind,
            type(err).__name__,
            (time.monotonic() - started) * 1000,
        )
        return False
    if status is ReviewJobStatus.RETRYING:
        await _log_retrying(jobs, claimed, log, started, error_kind=error_kind)
        return False
    if status is ReviewJobStatus.FAILED:
        log.warning(
            "review job failed: job_id=%s provider=%s repo=%s pr_number=%s "
            "error_kind=%s exc_class=%s duration_ms=%.0f",
            claimed.job_id,
            payload.provider,
            payload.repo_full_name,
            payload.pr_number,
            error_kind,
            exc_class,
            (time.monotonic() - started) * 1000,
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
    payload = claimed.payload
    next_attempt_at: datetime | None = None
    try:
        stored = await jobs.get(claimed.job_id)
        if stored is not None:
            next_attempt_at = stored.next_attempt_at
    except Exception as err:
        log.error(
            "review job store error on retry record: job_id=%s provider=%s repo=%s "
            "pr_number=%s error_kind=%s exc_class=%s duration_ms=%.0f",
            claimed.job_id,
            payload.provider,
            payload.repo_full_name,
            payload.pr_number,
            error_kind,
            type(err).__name__,
            (time.monotonic() - started) * 1000,
        )
    log.info(
        "review job retrying: job_id=%s provider=%s repo=%s pr_number=%s "
        "error_kind=%s attempt=%d next_attempt_at=%s duration_ms=%.0f",
        claimed.job_id,
        payload.provider,
        payload.repo_full_name,
        payload.pr_number,
        error_kind,
        claimed.attempt + 1,
        next_attempt_at.isoformat() if next_attempt_at is not None else "-",
        (time.monotonic() - started) * 1000,
    )
