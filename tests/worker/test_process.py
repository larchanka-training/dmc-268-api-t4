"""worker.process_review_job: fetch → parse → filter → stats, run inline.

This is Step 5 of docs/WORKFLOW_DESIGN.md §2, started as a background task by
the webhook (tasks/plan.md decision 2, 2026-10-07). All fakes are local and
never touch the network; clocks are injected, no sleeps.
"""

import logging

import pytest

from adapters.jobs.memory import MemoryJobStore
from domain.errors import (
    ForgeUnavailableError,
    InstallationGoneError,
    WebhookPayloadError,
)
from domain.ports import (
    CommitInfo,
    JobRepository,
    JobStats,
    NewReviewJob,
    PullRequestContext,
    ReviewJob,
    ReviewJobStatus,
)
from worker.process import process_review_job

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


class StubJobRepository:
    """Wraps a real store; terminal transitions can be stubbed out.

    mark_completed returns ``completed_result`` (False by default: every job
    reports superseded at completion) or raises ``completed_error`` instead of
    delegating; mark_failed returns ``failed_result`` or raises
    ``failed_error`` when set.
    """

    def __init__(
        self,
        inner: JobRepository,
        *,
        completed_result: bool = False,
        completed_error: Exception | None = None,
        failed_result: bool | None = None,
        failed_error: Exception | None = None,
    ) -> None:
        self._inner = inner
        self._completed_result = completed_result
        self._completed_error = completed_error
        self._failed_result = failed_result
        self._failed_error = failed_error

    async def enqueue(self, job: NewReviewJob) -> str | None:
        return await self._inner.enqueue(job)

    async def mark_processing(self, job_id: str) -> bool:
        return await self._inner.mark_processing(job_id)

    async def mark_completed(self, job_id: str, stats: JobStats) -> bool:
        if self._completed_error is not None:
            raise self._completed_error
        if not self._completed_result:
            return False
        return await self._inner.mark_completed(job_id, stats)

    async def mark_failed(self, job_id: str, error_kind: str) -> bool:
        if self._failed_error is not None:
            raise self._failed_error
        if self._failed_result is not None:
            return self._failed_result
        return await self._inner.mark_failed(job_id, error_kind)

    async def get(self, job_id: str) -> ReviewJob | None:
        return await self._inner.get(job_id)


async def enqueue(store: JobRepository, payload: NewReviewJob) -> str:
    job_id = await store.enqueue(payload)
    assert job_id is not None
    return job_id


# --- happy path ------------------------------------------------------------


async def test_completed_job_carries_exact_stats() -> None:
    store = MemoryJobStore()
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    payload = make_payload()
    job_id = await enqueue(store, payload)

    assert await process_review_job(job_id, payload, vcs, store) is True

    job = await store.get(job_id)
    assert job is not None
    assert job.status is ReviewJobStatus.COMPLETED
    assert job.stats == EXPECTED_STATS
    assert job.error_kind is None
    assert job.finished_at is not None
    assert vcs.calls == [(42, "octocat/hello-world", 7)]


async def test_presuperseded_job_never_reaches_the_forge() -> None:
    store = MemoryJobStore()
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    payload = make_payload(delivery_id="d-1")
    job_id = await enqueue(store, payload)
    assert await store.enqueue(make_payload(delivery_id="d-2")) is not None

    assert await process_review_job(job_id, payload, vcs, store) is False
    assert vcs.calls == []


# --- failure paths ---------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ForgeUnavailableError("github", "connection reset by peer"),
        InstallationGoneError(42, "uninstalled"),
        WebhookPayloadError("github", "missing pull_request field"),
    ],
)
async def test_forge_error_marks_job_failed_with_class_name(error: Exception) -> None:
    store = MemoryJobStore()
    vcs = FakeGitProvider(error=error)
    payload = make_payload()
    job_id = await enqueue(store, payload)

    assert await process_review_job(job_id, payload, vcs, store) is False

    job = await store.get(job_id)
    assert job is not None
    assert job.status is ReviewJobStatus.FAILED
    assert job.error_kind == type(error).__name__
    assert job.stats is None
    assert job.finished_at is not None


async def test_malformed_diff_marks_diff_parse_failed() -> None:
    store = MemoryJobStore()
    vcs = FakeGitProvider(make_context("this is not a unified diff"))
    payload = make_payload()
    job_id = await enqueue(store, payload)

    assert await process_review_job(job_id, payload, vcs, store) is False

    job = await store.get(job_id)
    assert job is not None
    assert job.status is ReviewJobStatus.FAILED
    assert job.error_kind == "diff_parse_failed"


async def test_unexpected_error_marks_internal_and_does_not_raise() -> None:
    store = MemoryJobStore()
    vcs = FakeGitProvider(error=RuntimeError(f"boom {DIFF_SENTINEL}"))
    payload = make_payload()
    job_id = await enqueue(store, payload)

    assert await process_review_job(job_id, payload, vcs, store) is False

    job = await store.get(job_id)
    assert job is not None
    assert job.status is ReviewJobStatus.FAILED
    assert job.error_kind == "internal"


async def test_supersession_mid_flight_returns_false_without_failing_job() -> None:
    store = MemoryJobStore()
    stub = StubJobRepository(store)
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    payload = make_payload()
    job_id = await enqueue(stub, payload)

    assert await process_review_job(job_id, payload, vcs, stub) is False

    job = await store.get(job_id)
    assert job is not None
    assert job.error_kind is None


async def test_store_error_on_completion_is_swallowed(caplog) -> None:
    store = MemoryJobStore()
    stub = StubJobRepository(store, completed_error=RuntimeError(f"store down {DIFF_SENTINEL}"))
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    payload = make_payload()
    job_id = await enqueue(stub, payload)

    with caplog.at_level(logging.ERROR, logger="worker.process"):
        assert await process_review_job(job_id, payload, vcs, stub) is False

    job = await store.get(job_id)
    assert job is not None
    assert job.status is ReviewJobStatus.PROCESSING
    assert "exc_class=RuntimeError" in caplog.text
    assert DIFF_SENTINEL not in caplog.text


async def test_store_error_while_recording_failure_is_swallowed() -> None:
    store = MemoryJobStore()
    stub = StubJobRepository(store, failed_error=RuntimeError("store down"))
    vcs = FakeGitProvider(error=ForgeUnavailableError("github", "connection reset by peer"))
    payload = make_payload()
    job_id = await enqueue(stub, payload)

    assert await process_review_job(job_id, payload, vcs, stub) is False

    job = await store.get(job_id)
    assert job is not None
    assert job.status is ReviewJobStatus.PROCESSING
    assert job.error_kind is None


async def test_failure_record_refused_leaves_job_unfailed() -> None:
    store = MemoryJobStore()
    stub = StubJobRepository(store, completed_result=True, failed_result=False)
    vcs = FakeGitProvider(error=ForgeUnavailableError("github", "connection reset by peer"))
    payload = make_payload()
    job_id = await enqueue(stub, payload)

    assert await process_review_job(job_id, payload, vcs, stub) is False

    job = await store.get(job_id)
    assert job is not None
    assert job.status is ReviewJobStatus.PROCESSING
    assert job.error_kind is None
    assert job.stats is None


# --- logs: counts in, content out ------------------------------------------


async def test_success_log_carries_counts_never_content(caplog) -> None:
    store = MemoryJobStore()
    vcs = FakeGitProvider(make_context(GOOD_DIFF))
    payload = make_payload()
    job_id = await enqueue(store, payload)

    with caplog.at_level(logging.INFO, logger="worker.process"):
        assert await process_review_job(job_id, payload, vcs, store) is True

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


async def test_failure_log_carries_error_kind_never_messages(caplog) -> None:
    store = MemoryJobStore()
    vcs = FakeGitProvider(error=ForgeUnavailableError("github", "connection reset by peer"))
    payload = make_payload()
    job_id = await enqueue(store, payload)

    with caplog.at_level(logging.WARNING, logger="worker.process"):
        assert await process_review_job(job_id, payload, vcs, store) is False

    text = caplog.text
    assert job_id in text
    assert "error_kind=ForgeUnavailableError" in text
    assert "connection reset" not in text
    assert DIFF_SENTINEL not in text
    assert TITLE_SENTINEL not in text
    assert DESCRIPTION_SENTINEL not in text
