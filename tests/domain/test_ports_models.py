from datetime import UTC, datetime

import pytest

from domain.ports import (
    ClaimedJob,
    CommitInfo,
    JobStats,
    NewReviewJob,
    PullRequestContext,
    ReviewJob,
    ReviewJobStatus,
)

HEAD_SHA = "a1b2c3d4e5" * 4
BASE_SHA = "f6e5d4c3b2" * 4
BAD_SHAS = ["", "a" * 39, "a" * 41, "A" * 40, "z" * 40, "not-a-sha"]


def make_commit(sha: str = HEAD_SHA) -> CommitInfo:
    return CommitInfo(sha=sha, message="Fix a thing", author_login="octocat")


def make_context(**overrides: object) -> PullRequestContext:
    values: dict[str, object] = {
        "installation_id": 42,
        "repo_full_name": "octocat/hello-world",
        "pr_number": 7,
        "title": "Add a feature",
        "description": "It does a thing",
        "head_sha": HEAD_SHA,
        "base_sha": BASE_SHA,
        "head_ref": "feature",
        "base_ref": "main",
        "author_login": "octocat",
        "author_external_id": 1234,
        "author_association": "CONTRIBUTOR",
        "commits": (make_commit(), make_commit(BASE_SHA)),
        "diff_text": "diff --git a/x.py b/x.py",
    }
    values.update(overrides)
    return PullRequestContext(**values)  # type: ignore[arg-type]


def make_stats(**overrides: object) -> JobStats:
    values: dict[str, object] = {
        "files_total": 4,
        "files_reviewable": 2,
        "hunks_total": 5,
        "chunks_total": 2,
        "skipped_counts": (("lockfile", 1), ("binary", 1)),
    }
    values.update(overrides)
    return JobStats(**values)  # type: ignore[arg-type]


def make_new_job(**overrides: object) -> NewReviewJob:
    values: dict[str, object] = {
        "provider": "github",
        "delivery_id": "d-6f1c2a30-51b1-11ef",
        "installation_id": 42,
        "repo_id": 751065667,
        "repo_full_name": "octocat/hello-world",
        "pr_number": 7,
        "head_sha": HEAD_SHA,
        "base_sha": BASE_SHA,
        "event_action": "opened",
        "pr_title": "Add a feature",
    }
    values.update(overrides)
    return NewReviewJob(**values)  # type: ignore[arg-type]


def make_review_job(**overrides: object) -> ReviewJob:
    values: dict[str, object] = {
        "job_id": "0199b6af-1f3e-7c19-9aae-9db6f4c8e6d0",
        "created_at": datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
        "status": ReviewJobStatus.QUEUED,
        "payload": make_new_job(),
    }
    values.update(overrides)
    return ReviewJob(**values)  # type: ignore[arg-type]


# --- CommitInfo ---------------------------------------------------------


def test_commit_info_happy_path() -> None:
    commit = make_commit()

    assert commit.sha == HEAD_SHA
    assert commit.message == "Fix a thing"
    assert commit.author_login == "octocat"


def test_commit_info_allows_an_empty_message_and_a_missing_author() -> None:
    commit = CommitInfo(sha=BASE_SHA, message="", author_login=None)

    assert commit.message == ""
    assert commit.author_login is None


@pytest.mark.parametrize("sha", BAD_SHAS)
def test_commit_info_rejects_a_bad_sha(sha: str) -> None:
    with pytest.raises(ValueError, match="sha"):
        CommitInfo(sha=sha, message="m", author_login=None)


# --- PullRequestContext -------------------------------------------------


def test_pull_request_context_happy_path() -> None:
    context = make_context()

    assert context.installation_id == 42
    assert context.pr_number == 7
    assert context.head_sha == HEAD_SHA
    assert context.base_sha == BASE_SHA
    assert len(context.commits) == 2


def test_pull_request_context_repr_hides_the_diff() -> None:
    context = make_context(diff_text="diff --git a/SECRET_PATH.py b/SECRET_PATH.py")

    assert "SECRET_PATH.py" not in repr(context)


def test_pull_request_context_allows_an_anonymous_author() -> None:
    context = make_context(author_login=None, author_external_id=None, author_association=None)

    assert context.author_login is None
    assert context.author_external_id is None
    assert context.author_association is None


@pytest.mark.parametrize("field", ["head_sha", "base_sha"])
@pytest.mark.parametrize("sha", BAD_SHAS)
def test_pull_request_context_rejects_a_bad_sha(field: str, sha: str) -> None:
    with pytest.raises(ValueError, match="sha"):
        make_context(**{field: sha})


@pytest.mark.parametrize("pr_number", [0, -3])
def test_pull_request_context_rejects_a_non_positive_pr_number(pr_number: int) -> None:
    with pytest.raises(ValueError, match="pr_number"):
        make_context(pr_number=pr_number)


@pytest.mark.parametrize("installation_id", [0, -1])
def test_pull_request_context_rejects_a_non_positive_installation_id(
    installation_id: int,
) -> None:
    with pytest.raises(ValueError, match="installation_id"):
        make_context(installation_id=installation_id)


@pytest.mark.parametrize(
    "repo_full_name",
    ["octocat", "octocat/", "/hello-world", "octocat/hello/world", " "],
)
def test_pull_request_context_rejects_a_malformed_repo_full_name(repo_full_name: str) -> None:
    with pytest.raises(ValueError, match="owner/name"):
        make_context(repo_full_name=repo_full_name)


# --- ReviewJobStatus ----------------------------------------------------


def test_review_job_status_values() -> None:
    assert [status.value for status in ReviewJobStatus] == [
        "QUEUED",
        "PROCESSING",
        "RETRYING",
        "COMPLETED",
        "FAILED",
        "SUPERSEDED",
        "SKIPPED",
    ]


# --- JobStats -----------------------------------------------------------


def test_job_stats_happy_path() -> None:
    stats = make_stats()

    assert stats.files_total == 4
    assert stats.files_reviewable == 2
    assert stats.hunks_total == 5
    assert stats.chunks_total == 2
    assert stats.skipped_counts == (("lockfile", 1), ("binary", 1))


def test_job_stats_allows_empty_skips() -> None:
    stats = JobStats(
        files_total=3, files_reviewable=3, hunks_total=4, chunks_total=1, skipped_counts=()
    )

    assert stats.skipped_counts == ()


@pytest.mark.parametrize(
    "field",
    ["files_total", "files_reviewable", "hunks_total", "chunks_total"],
)
def test_job_stats_rejects_a_negative_count(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        make_stats(**{field: -1})


def test_job_stats_rejects_more_reviewable_files_than_files() -> None:
    with pytest.raises(ValueError, match="files_reviewable"):
        make_stats(files_total=2, files_reviewable=3)


def test_job_stats_rejects_a_duplicate_skip_reason() -> None:
    with pytest.raises(ValueError, match="unique"):
        make_stats(skipped_counts=(("lockfile", 1), ("lockfile", 2)))


def test_job_stats_rejects_a_zero_skip_count() -> None:
    with pytest.raises(ValueError, match=">= 1"):
        make_stats(skipped_counts=(("lockfile", 0),))


def test_job_stats_rejects_an_empty_skip_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        make_stats(skipped_counts=((" ", 1),))


# --- NewReviewJob -------------------------------------------------------


def test_new_review_job_happy_path() -> None:
    job = make_new_job()

    assert job.provider == "github"
    assert job.delivery_id == "d-6f1c2a30-51b1-11ef"
    assert job.event_action == "opened"
    assert job.head_sha == HEAD_SHA
    assert job.pr_title == "Add a feature"


def test_new_review_job_d5_defaults_are_none() -> None:
    job = make_new_job()

    assert job.author_login is None
    assert job.head_ref is None
    assert job.base_ref is None


def test_new_review_job_accepts_d5_metadata_and_an_empty_title() -> None:
    job = make_new_job(
        pr_title="",
        author_login="octocat",
        head_ref="feature",
        base_ref="main",
    )

    assert job.pr_title == ""
    assert job.author_login == "octocat"
    assert job.head_ref == "feature"
    assert job.base_ref == "main"


def test_new_review_job_allows_a_missing_delivery_id() -> None:
    assert make_new_job(delivery_id=None).delivery_id is None


@pytest.mark.parametrize("provider", ["", "  "])
def test_new_review_job_rejects_an_empty_provider(provider: str) -> None:
    with pytest.raises(ValueError, match="provider"):
        make_new_job(provider=provider)


@pytest.mark.parametrize("event_action", ["", "  "])
def test_new_review_job_rejects_an_empty_event_action(event_action: str) -> None:
    with pytest.raises(ValueError, match="event_action"):
        make_new_job(event_action=event_action)


@pytest.mark.parametrize("field", ["installation_id", "repo_id", "pr_number"])
@pytest.mark.parametrize("value", [0, -1])
def test_new_review_job_rejects_a_non_positive_id(field: str, value: int) -> None:
    with pytest.raises(ValueError, match=field):
        make_new_job(**{field: value})


@pytest.mark.parametrize("field", ["head_sha", "base_sha"])
@pytest.mark.parametrize("sha", BAD_SHAS)
def test_new_review_job_rejects_a_bad_sha(field: str, sha: str) -> None:
    with pytest.raises(ValueError, match="sha"):
        make_new_job(**{field: sha})


# --- ReviewJob ----------------------------------------------------------


def test_review_job_happy_path() -> None:
    created_at = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    finished_at = datetime(2026, 10, 7, 12, 1, tzinfo=UTC)
    job = make_review_job(
        status=ReviewJobStatus.COMPLETED,
        stats=make_stats(),
        error_kind=None,
        finished_at=finished_at,
    )

    assert job.job_id == "0199b6af-1f3e-7c19-9aae-9db6f4c8e6d0"
    assert job.created_at == created_at
    assert job.finished_at == finished_at
    assert job.stats is not None
    assert job.stats.files_total == 4


def test_review_job_defaults_to_no_stats_error_or_finish_time() -> None:
    job = make_review_job()

    assert job.stats is None
    assert job.error_kind is None
    assert job.finished_at is None
    assert job.retry_count == 0
    assert job.next_attempt_at is None


def test_review_job_carries_retry_state() -> None:
    next_attempt_at = datetime(2026, 10, 7, 12, 3, tzinfo=UTC)
    job = make_review_job(
        status=ReviewJobStatus.RETRYING,
        retry_count=2,
        next_attempt_at=next_attempt_at,
    )

    assert job.retry_count == 2
    assert job.next_attempt_at == next_attempt_at


@pytest.mark.parametrize("job_id", ["", "  "])
def test_review_job_rejects_an_empty_job_id(job_id: str) -> None:
    with pytest.raises(ValueError, match="job_id"):
        make_review_job(job_id=job_id)


# --- ClaimedJob ----------------------------------------------------------


def test_claimed_job_carries_the_claim_state() -> None:
    claimed = ClaimedJob(job_id="job-1", payload=make_new_job(), attempt=2, worker_id="w-1")

    assert claimed.job_id == "job-1"
    assert claimed.payload.pr_title == "Add a feature"
    assert claimed.attempt == 2
    assert claimed.worker_id == "w-1"
