"""worker.process_claimed_job with a ReviewStage: parsed diff → LLM → posted review."""

from adapters.jobs.memory import MemoryJobStore
from domain.errors import AllProvidersFailedError, ProviderUnavailableError
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
    PublishOutcome,
    PullRequestTarget,
    ReviewJobStatus,
    ReviewRequest,
)
from domain.publishing import PublishPlan
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
