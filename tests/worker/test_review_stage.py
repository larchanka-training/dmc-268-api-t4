"""worker.process_claimed_job with a ReviewStage: parsed diff → LLM → posted review."""

from collections.abc import Sequence

import pytest

from adapters.jobs.memory import MemoryJobStore
from adapters.memory.review_comments import MemoryReviewCommentStore
from domain.errors import AllProvidersFailedError, ProviderUnavailableError
from domain.findings import NewPublishedFinding
from domain.jobs import JobStep, StepStatus
from domain.models import (
    Category,
    DiffSide,
    Finding,
    FindingPosition,
    ReviewResult,
    Severity,
)
from domain.ports import (
    ExistingCheckRun,
    PublishedComment,
    PublishOutcome,
    PullRequestTarget,
    ReviewJobStatus,
    ReviewRequest,
)
from domain.publishing import PublishPlan
from tests.conftest import all_log_text, log_events
from tests.worker.test_process import (
    DIFF_SENTINEL,
    GOOD_DIFF,
    HEAD_SHA,
    FakeGitProvider,
    claim,
    enqueue,
    make_context,
    make_payload,
)
from worker.process import ReviewStage, process_claimed_job


def finding(path: str, line: int) -> Finding:
    return Finding(
        position=FindingPosition(path=path, line=line, side=DiffSide.NEW),
        severity=Severity.HIGH,
        category=Category.SECURITY,
        message="Hard-coded value",
        suggestion=None,
    )


class FakeGateway:
    def __init__(self, findings: tuple[Finding, ...] = (), error: Exception | None = None):
        self.findings = findings
        self.error = error
        self.requests: list[ReviewRequest] = []

    async def review(self, request: ReviewRequest) -> ReviewResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ReviewResult(summary="One issue.", findings=self.findings, provider="p", model="m")


class FakePublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[PullRequestTarget, PublishPlan]] = []

    async def publish(
        self,
        target: PullRequestTarget,
        plan: PublishPlan,
        existing_check: ExistingCheckRun | None = None,
    ) -> PublishOutcome:
        self.calls.append((target, plan))
        return PublishOutcome(
            check_run_id=1, check_run_url=None, review_id=2, review_url=None, comments=()
        )


async def test_findings_on_diff_lines_are_posted_to_the_head_commit() -> None:
    store = MemoryJobStore()
    await enqueue(store, make_payload())
    claimed = await claim(store)
    # Line 2 of app/users.py is an added line; line 40 is not in the diff.
    gateway = FakeGateway((finding("app/users.py", 2), finding("app/users.py", 40)))
    publisher = FakePublisher()

    done = await process_claimed_job(
        claimed,
        FakeGitProvider(make_context(GOOD_DIFF)),
        store,
        review=ReviewStage(gateway=gateway, publisher=publisher),
    )

    assert done is True
    assert DIFF_SENTINEL in gateway.requests[0].diff_text
    [(target, plan)] = publisher.calls
    assert (target.owner, target.repo, target.number, target.head_sha) == (
        "octocat",
        "hello-world",
        7,
        HEAD_SHA,
    )
    assert [c.finding.position.line for c in plan.comments] == [2]


async def test_an_unavailable_model_retries_and_posts_nothing() -> None:
    store = MemoryJobStore()
    job_id = await enqueue(store, make_payload())
    claimed = await claim(store)
    publisher = FakePublisher()
    error = AllProvidersFailedError([ProviderUnavailableError("p", "down")])

    done = await process_claimed_job(
        claimed,
        FakeGitProvider(make_context(GOOD_DIFF)),
        store,
        review=ReviewStage(gateway=FakeGateway(error=error), publisher=publisher),
    )

    assert done is False
    assert publisher.calls == []
    stored = await store.get(job_id)
    assert stored is not None and stored.status is ReviewJobStatus.RETRYING


class FakeStepRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def start(self, job_id: str, attempt: int, step: JobStep, *, worker_id: str) -> None:
        self.calls.append((str(step), "RUNNING", None))

    async def finish(
        self,
        job_id: str,
        attempt: int,
        step: JobStep,
        status: StepStatus,
        *,
        worker_id: str,
        error_kind: str | None = None,
    ) -> None:
        self.calls.append((str(step), str(status), error_kind))


async def test_each_step_is_recorded_as_it_starts_and_ends() -> None:
    store = MemoryJobStore()
    await enqueue(store, make_payload())
    claimed = await claim(store)
    steps = FakeStepRecorder()

    await process_claimed_job(
        claimed,
        FakeGitProvider(make_context(GOOD_DIFF)),
        store,
        review=ReviewStage(gateway=FakeGateway(), publisher=FakePublisher()),
        steps=steps,
    )

    assert steps.calls == [
        ("CLAIMED", "RUNNING", None),
        ("CLAIMED", "OK", None),
        ("FETCH_DIFF", "RUNNING", None),
        ("FETCH_DIFF", "OK", None),
        ("LLM_CALL", "RUNNING", None),
        ("LLM_CALL", "OK", None),
        ("POST_FEEDBACK", "RUNNING", None),
        ("POST_FEEDBACK", "OK", None),
    ]


async def test_the_failing_step_is_recorded_with_its_error_kind() -> None:
    store = MemoryJobStore()
    await enqueue(store, make_payload())
    claimed = await claim(store)
    steps = FakeStepRecorder()
    error = AllProvidersFailedError([ProviderUnavailableError("p", "down")])

    await process_claimed_job(
        claimed,
        FakeGitProvider(make_context(GOOD_DIFF)),
        store,
        review=ReviewStage(gateway=FakeGateway(error=error), publisher=FakePublisher()),
        steps=steps,
    )

    assert steps.calls[-1] == ("LLM_CALL", "FAILED", "provider_unavailable")


class BrokenStepRecorder(FakeStepRecorder):
    async def start(self, job_id: str, attempt: int, step: JobStep, *, worker_id: str) -> None:
        raise ConnectionError("database restarting")


async def test_a_failed_step_write_does_not_stop_the_review() -> None:
    store = MemoryJobStore()
    await enqueue(store, make_payload())
    claimed = await claim(store)
    publisher = FakePublisher()

    done = await process_claimed_job(
        claimed,
        FakeGitProvider(make_context(GOOD_DIFF)),
        store,
        review=ReviewStage(gateway=FakeGateway(), publisher=publisher),
        steps=BrokenStepRecorder(),
    )

    assert done is True
    assert len(publisher.calls) == 1


# --- published findings -----------------------------------------------------


class ReportingPublisher:
    """Reports every planned comment back as posted, like a forge that took them all."""

    def __init__(self) -> None:
        self.plans: list[PublishPlan] = []

    async def publish(
        self,
        target: PullRequestTarget,
        plan: PublishPlan,
        existing_check: ExistingCheckRun | None = None,
    ) -> PublishOutcome:
        self.plans.append(plan)
        return PublishOutcome(
            check_run_id=1,
            check_run_url=None,
            review_id=900,
            review_url=None,
            comments=tuple(
                PublishedComment(comment.fingerprint, 5000 + index, None)
                for index, comment in enumerate(plan.comments)
            ),
        )


class BrokenCommentStore(MemoryReviewCommentStore):
    async def record(self, job_id: str, findings: Sequence[NewPublishedFinding]) -> None:
        raise RuntimeError("database went away")


async def _review_once(
    store: MemoryJobStore, comments: MemoryReviewCommentStore, delivery_id: str
) -> tuple[str, ReportingPublisher, bool]:
    job_id = await enqueue(store, make_payload(delivery_id=delivery_id))
    claimed = await claim(store)
    publisher = ReportingPublisher()
    done = await process_claimed_job(
        claimed,
        FakeGitProvider(make_context(GOOD_DIFF)),
        store,
        review=ReviewStage(
            gateway=FakeGateway((finding("app/users.py", 2),)),
            publisher=publisher,
            comments=comments,
        ),
    )
    return job_id, publisher, done


async def test_posted_findings_are_kept_and_not_posted_again_on_the_same_pull_request() -> None:
    store = MemoryJobStore()
    comments = MemoryReviewCommentStore(store)

    first_id, first, done = await _review_once(store, comments, "d-1")

    assert done is True
    [kept] = await comments.for_job(first_id)
    [planned] = first.plans[0].comments
    assert (kept.fingerprint, kept.path, kept.line) == (planned.fingerprint, "app/users.py", 2)
    assert (kept.external_comment_id, kept.external_review_id) == (5000, 900)
    assert kept.message == planned.body

    second_id, second, done = await _review_once(store, comments, "d-2")

    assert done is True
    assert second.plans[0].comments == ()
    assert second.plans[0].stats.already_posted == 1
    assert await comments.for_job(second_id) == ()


async def test_a_store_failure_after_posting_still_completes_the_job(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = MemoryJobStore()

    with caplog.at_level("ERROR"):
        job_id, publisher, done = await _review_once(store, BrokenCommentStore(store), "d-1")

    assert done is True
    assert len(publisher.plans) == 1
    stored = await store.get(job_id)
    assert stored is not None and stored.status is ReviewJobStatus.COMPLETED
    [record] = log_events(caplog, "job.findings.not_kept")
    assert record.error_type == "RuntimeError"
    assert "database went away" not in all_log_text(caplog)
