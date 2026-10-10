"""worker.process_claimed_job: fetch → parse → filter → stats, on a claim.

This is Step 5 of docs/WORKFLOW_DESIGN.md §2, run by the worker claim loop
(§2 Step 4) — here a test claims through the real in-memory store first. All
fakes are local and never touch the network; clocks are injected, no sleeps.
"""

import logging
from datetime import UTC, datetime

import pytest

from adapters.jobs.memory import MemoryJobStore
from domain.errors import (
    ForgeUnavailableError,
    InstallationGoneError,
    PrNotFoundError,
)
from domain.ports import (
    ClaimedJob,
    CommitInfo,
    JobRepository,
    JobStats,
    NewReviewJob,
    PullRequestContext,
    ReviewJob,
    ReviewJobStatus,
)
from worker.process import process_claimed_job

HEAD_SHA = "a1b2c3d4e5" * 4
BASE_SHA = "f6e5d4c3b2" * 4

TITLE_SENTINEL = "pr-title-SENTINEL-4f0a"
DESCRIPTION_SENTINEL = "pr-description-SENTINEL-4f0b"
DIFF_SENTINEL = "diff-sentinel-4f0c"

GOOD_DIFF = (
    "diff --git a/app/users.py b/app/users.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/app/users.py\n"
    "+++ b/app/users.py\n"
    "@@ -1,2 +1,3 @@\n"
    " base\n"
    "-gone\n"
    f"+{DIFF_SENTINEL}\n"
    "+more\n"
    "diff --git a/package-lock.json b/package-lock.json\n"
    "index 3333333..4444444 100644\n"
    "--- a/package-lock.json\n"
    "+++ b/package-lock.json\n"
    "@@ -1,3 +1,3 @@\n"
    " {\n"
    '-  "a": 1\n'
    '+  "a": 2\n'
    " }\n"
)

EXPECTED_STATS = JobStats(
    files_total=2,
    files_reviewable=1,
    hunks_total=2,
    chunks_total=1,
    skipped_counts=(("lockfile", 1),),
)


def make_payload(**overrides: object) -> NewReviewJob:
    values: dict[str, object] = {
        "provider": "github",
        "delivery_id": "d-1",
        "installation_id": 42,
        "repo_id": 751065667,
        "repo_full_name": "octocat/hello-world",
        "pr_number": 7,
        "head_sha": HEAD_SHA,
        "base_sha": BASE_SHA,
        "event_action": "opened",
        "pr_title": TITLE_SENTINEL,
    }
    values.update(overrides)
    return NewReviewJob(**values)  # type: ignore[arg-type]


def make_context(diff_text: str) -> PullRequestContext:
    return PullRequestContext(
        installation_id=42,
        repo_full_name="octocat/hello-world",
        pr_number=7,
        title=TITLE_SENTINEL,
        description=DESCRIPTION_SENTINEL,
        head_sha=HEAD_SHA,
        base_sha=BASE_SHA,
        head_ref="feature",
        base_ref="main",
        author_login="octocat",
        author_external_id=1234,
        author_association="CONTRIBUTOR",
        commits=(CommitInfo(sha=HEAD_SHA, message="Fix a thing", author_login="octocat"),),
        diff_text=diff_text,
    )


class FakeGitProvider:
    """Records calls and returns a canned context; never touches the network."""

    def __init__(
        self, context: PullRequestContext | None = None, *, error: Exception | None = None
    ) -> None:
        self.context = context
        self.error = error
        self.calls: list[tuple[int, str, int]] = []

    async def fetch_pull_request(
        self, installation_id: int, repo_full_name: str, number: int
    ) -> PullRequestContext:
        self.calls.append((installation_id, repo_full_name, number))
        if self.error is not None:
            raise self.error
        assert self.context is not None
        return self.context


_MISSING = object()


class StubJobRepository:
    """Wraps a real store; terminal transitions can be stubbed out.

    complete returns ``completed_result`` (False by default: every job
    reports superseded at completion) or raises ``completed_error`` instead
    of delegating; fail returns ``failed_result`` (MISSING by default:
    delegate to the store) or raises ``failed_error`` when set.
    """

    def __init__(
        self,
        inner: JobRepository,
        *,
        completed_result: bool = False,
        completed_error: Exception | None = None,
        failed_result: object = _MISSING,
        failed_error: Exception | None = None,
        get_result: object = _MISSING,
    ) -> None:
        self._inner = inner
        self._completed_result = completed_result
        self._completed_error = completed_error
        self._failed_result = failed_result
        self._failed_error = failed_error
        self._get_result = get_result

    async def enqueue(self, job: NewReviewJob) -> str | None:
        return await self._inner.enqueue(job)

    async def claim(self, worker_id: str) -> ClaimedJob | None:
        return await self._inner.claim(worker_id)

    async def complete(self, job_id: str, stats: JobStats, *, worker_id: str) -> bool:
        if self._completed_error is not None:
            raise self._completed_error
        if not self._completed_result:
            return False
        return await self._inner.complete(job_id, stats, worker_id=worker_id)

    async def fail(
        self, job_id: str, error_kind: str, *, retryable: bool, worker_id: str
    ) -> ReviewJobStatus | None:
        if self._failed_error is not None:
            raise self._failed_error
        if self._failed_result is not _MISSING:
            return self._failed_result  # type: ignore[no-any-return]
        return await self._inner.fail(job_id, error_kind, retryable=retryable, worker_id=worker_id)

    async def extend_lease(self, job_id: str, *, worker_id: str) -> bool:
        return await self._inner.extend_lease(job_id, worker_id=worker_id)

    async def get(self, job_id: str) -> ReviewJob | None:
        if self._get_result is not _MISSING:
            return self._get_result  # type: ignore[no-any-return]
        return await self._inner.get(job_id)


async def enqueue(store: JobRepository, payload: NewReviewJob) -> str:
    job_id = await store.enqueue(payload)
    assert job_id is not None
    return job_id


async def claim(store: JobRepository, worker_id: str = "w-1") -> ClaimedJob:
    claimed = await store.claim(worker_id)
    assert claimed is not None
    return claimed


# --- happy path ------------------------------------------------------------


async def test_completed_job_carries_exact_stats() -> None:
    store = MemoryJobStore()
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    payload = make_payload()
    job_id = await enqueue(store, payload)
    claimed = await claim(store)

    assert await process_claimed_job(claimed, vcs, store) is True

    job = await store.get(job_id)
    assert job is not None
    assert job.status is ReviewJobStatus.COMPLETED
    assert job.stats == EXPECTED_STATS
    assert job.error_kind is None
    assert job.finished_at is not None
    assert vcs.calls == [(42, "octocat/hello-world", 7)]


async def test_a_superseded_job_is_never_claimed() -> None:
    store = MemoryJobStore()
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    first = await enqueue(store, make_payload(delivery_id="d-1"))
    await enqueue(store, make_payload(delivery_id="d-2"))

    claimed = await claim(store)
    assert claimed.payload.delivery_id == "d-2"
    assert await process_claimed_job(claimed, vcs, store) is True

    superseded = await store.get(first)
    assert superseded is not None
    assert superseded.status is ReviewJobStatus.SUPERSEDED
    # The forge saw exactly one fetch — for the surviving job.
    assert vcs.calls == [(42, "octocat/hello-world", 7)]


# --- failure paths ---------------------------------------------------------


async def test_retryable_forge_error_sends_the_job_to_retrying() -> None:
    store = MemoryJobStore()
    vcs = FakeGitProvider(error=ForgeUnavailableError("github", "connection reset by peer"))
    job_id = await enqueue(store, make_payload())
    claimed = await claim(store)

    assert await process_claimed_job(claimed, vcs, store) is False

    job = await store.get(job_id)
    assert job is not None
    assert job.status is ReviewJobStatus.RETRYING
    assert job.error_kind == "forge_unavailable"
    assert job.retry_count == 1
    assert job.next_attempt_at is not None
    assert job.finished_at is None
    assert job.stats is None


@pytest.mark.parametrize(
    ("error", "expected_kind"),
    [
        (InstallationGoneError(42, "uninstalled"), "installation_removed"),
        (PrNotFoundError("github", "pull request not found"), "pr_not_found"),
        (RuntimeError(f"boom {DIFF_SENTINEL}"), "internal_error"),
    ],
)
async def test_permanent_errors_fail_the_job_immediately(
    error: Exception, expected_kind: str
) -> None:
    store = MemoryJobStore()
    vcs = FakeGitProvider(error=error)
    job_id = await enqueue(store, make_payload())
    claimed = await claim(store)

    assert await process_claimed_job(claimed, vcs, store) is False

    job = await store.get(job_id)
    assert job is not None
    assert job.status is ReviewJobStatus.FAILED
    assert job.error_kind == expected_kind
    assert job.retry_count == 0
    assert job.next_attempt_at is None
    assert job.finished_at is not None


async def test_malformed_diff_marks_diff_parse_failed() -> None:
    store = MemoryJobStore()
    vcs = FakeGitProvider(make_context("this is not a unified diff"))
    job_id = await enqueue(store, make_payload())
    claimed = await claim(store)

    assert await process_claimed_job(claimed, vcs, store) is False

    job = await store.get(job_id)
    assert job is not None
    assert job.status is ReviewJobStatus.FAILED
    assert job.error_kind == "diff_parse_failed"


async def test_lost_lease_at_completion_returns_false_without_failing_job() -> None:
    store = MemoryJobStore()
    stub = StubJobRepository(store)
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    job_id = await enqueue(stub, make_payload())
    claimed = await claim(stub)

    assert await process_claimed_job(claimed, vcs, stub) is False

    job = await store.get(job_id)
    assert job is not None
    assert job.error_kind is None


async def test_store_error_on_completion_is_swallowed(caplog) -> None:
    store = MemoryJobStore()
    stub = StubJobRepository(store, completed_error=RuntimeError(f"store down {DIFF_SENTINEL}"))
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    job_id = await enqueue(stub, make_payload())
    claimed = await claim(stub)

    with caplog.at_level(logging.ERROR, logger="worker.process"):
        assert await process_claimed_job(claimed, vcs, stub) is False

    job = await store.get(job_id)
    assert job is not None
    assert job.status is ReviewJobStatus.PROCESSING
    assert "exc_class=RuntimeError" in caplog.text
    assert DIFF_SENTINEL not in caplog.text


async def test_store_error_while_recording_failure_is_swallowed() -> None:
    store = MemoryJobStore()
    stub = StubJobRepository(store, failed_error=RuntimeError("store down"))
    vcs = FakeGitProvider(error=ForgeUnavailableError("github", "connection reset by peer"))
    job_id = await enqueue(stub, make_payload())
    claimed = await claim(stub)

    assert await process_claimed_job(claimed, vcs, stub) is False

    job = await store.get(job_id)
    assert job is not None
    assert job.status is ReviewJobStatus.PROCESSING
    assert job.error_kind is None


async def test_failure_record_refused_leaves_job_unfailed() -> None:
    store = MemoryJobStore()
    stub = StubJobRepository(store, completed_result=True, failed_result=None)
    vcs = FakeGitProvider(error=ForgeUnavailableError("github", "connection reset by peer"))
    job_id = await enqueue(stub, make_payload())
    claimed = await claim(stub)

    assert await process_claimed_job(claimed, vcs, stub) is False

    job = await store.get(job_id)
    assert job is not None
    assert job.status is ReviewJobStatus.PROCESSING
    assert job.error_kind is None
    assert job.stats is None


# --- logs: counts in, content out ------------------------------------------


async def test_success_log_carries_counts_never_content(
    caplog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEBUG_LOG_DIFF", raising=False)
    store = MemoryJobStore()
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    payload = make_payload()
    job_id = await enqueue(store, payload)
    claimed = await claim(store)

    with caplog.at_level(logging.INFO, logger="worker.process"):
        assert await process_claimed_job(claimed, vcs, store) is True

    text = caplog.text
    assert job_id in text
    assert payload.provider in text
    assert payload.repo_full_name in text
    assert "files=2" in text
    assert "reviewable=1" in text
    assert "hunks=2" in text
    assert "chunks=1" in text
    assert DIFF_SENTINEL not in text
    assert TITLE_SENTINEL not in text
    assert DESCRIPTION_SENTINEL not in text


async def test_failure_log_carries_error_kind_never_messages(
    caplog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEBUG_LOG_DIFF", raising=False)
    store = MemoryJobStore()
    vcs = FakeGitProvider(error=InstallationGoneError(42, f"permission revoked {DIFF_SENTINEL}"))
    payload = make_payload()
    job_id = await enqueue(store, payload)
    claimed = await claim(store)

    with caplog.at_level(logging.WARNING, logger="worker.process"):
        assert await process_claimed_job(claimed, vcs, store) is False

    text = caplog.text
    assert job_id in text
    assert "error_kind=installation_removed" in text
    assert "permission revoked" not in text
    assert DIFF_SENTINEL not in text
    assert TITLE_SENTINEL not in text
    assert DESCRIPTION_SENTINEL not in text


async def test_retrying_log_carries_the_retry_schedule_never_messages(
    caplog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEBUG_LOG_DIFF", raising=False)
    store = MemoryJobStore()
    vcs = FakeGitProvider(error=ForgeUnavailableError("github", "connection reset by peer"))
    payload = make_payload()
    job_id = await enqueue(store, payload)
    claimed = await claim(store)

    with caplog.at_level(logging.INFO, logger="worker.process"):
        assert await process_claimed_job(claimed, vcs, store) is False

    text = caplog.text
    assert "review job retrying:" in text
    assert f"job_id={job_id}" in text
    assert "error_kind=forge_unavailable" in text
    assert "attempt=1" in text
    assert "next_attempt_at=" in text
    assert "next_attempt_at=None" not in text
    assert "connection reset" not in text
    assert DIFF_SENTINEL not in text
    assert TITLE_SENTINEL not in text


async def test_retrying_log_tolerates_a_supersede_before_the_read_back(
    caplog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEBUG_LOG_DIFF", raising=False)
    store = MemoryJobStore()
    vcs = FakeGitProvider(error=ForgeUnavailableError("github", "connection reset by peer"))
    payload = make_payload()
    job_id = await enqueue(store, payload)
    claimed = await claim(store)
    # The race: a supersede lands between fail() and the read-back, so the
    # job comes back with no next_attempt_at — the log line says '-', not None.
    superseded = ReviewJob(
        job_id=job_id,
        created_at=datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
        status=ReviewJobStatus.SUPERSEDED,
        payload=payload,
    )
    stub = StubJobRepository(store, failed_result=ReviewJobStatus.RETRYING, get_result=superseded)

    with caplog.at_level(logging.INFO, logger="worker.process"):
        assert await process_claimed_job(claimed, vcs, stub) is False

    text = caplog.text
    assert "review job retrying:" in text
    assert "next_attempt_at=- " in text
    assert "next_attempt_at=None" not in text


async def test_the_file_inventory_is_logged_with_paths_and_reasons(caplog) -> None:
    store = MemoryJobStore()
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    await enqueue(store, make_payload())
    claimed = await claim(store)

    with caplog.at_level(logging.INFO, logger="worker.process"):
        assert await process_claimed_job(claimed, vcs, store) is True

    text = caplog.text
    assert "review job files:" in text
    assert "app/users.py" in text
    assert "package-lock.json (lockfile)" in text


async def test_the_debug_log_diff_flag_logs_the_diff_text(
    caplog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEBUG_LOG_DIFF", "1")
    store = MemoryJobStore()
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    await enqueue(store, make_payload())
    claimed = await claim(store)

    with caplog.at_level(logging.INFO, logger="worker.process"):
        assert await process_claimed_job(claimed, vcs, store) is True

    # The single sanctioned exception to the no-diff-in-logs rule: an explicit
    # dev-only flag, off by default.
    assert DIFF_SENTINEL in caplog.text


async def test_the_diff_stays_out_of_logs_without_the_flag(
    caplog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEBUG_LOG_DIFF", raising=False)
    store = MemoryJobStore()
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    await enqueue(store, make_payload())
    claimed = await claim(store)

    with caplog.at_level(logging.DEBUG, logger="worker.process"):
        assert await process_claimed_job(claimed, vcs, store) is True

    assert DIFF_SENTINEL not in caplog.text
