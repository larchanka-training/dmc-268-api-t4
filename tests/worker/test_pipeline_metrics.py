"""The pipeline's stages as metrics (docs/observability.md § Metrics, § Review pipeline):
the context built in a step of its own, the worker's Postgres calls by operation, and
the findings the forge accepted by severity."""

from adapters.jobs.memory import MemoryJobStore
from adapters.memory.review_comments import MemoryReviewCommentStore
from adapters.observability import metrics
from domain.errors import ERROR_KINDS
from domain.ports import (
    ExistingCheckRun,
    PublishedComment,
    PublishOutcome,
    PullRequestTarget,
)
from domain.publishing import PublishPlan
from tests.adapters.test_observability_observer import RecordingObserver
from tests.worker.test_process import (
    GOOD_DIFF,
    FakeGitProvider,
    claim,
    enqueue,
    make_context,
    make_payload,
)
from tests.worker.test_review_stage import FakeGateway, finding
from worker.loop import claim_and_process_once
from worker.process import RECORDED_STEPS, ReviewStage, process_claimed_job


def db_calls(operation: str) -> float:
    value = metrics.WORKER_REGISTRY.get_sample_value(
        "worker_db_duration_seconds_count", {"operation": operation}
    )
    return value or 0.0


def posted(severity: str) -> float:
    value = metrics.WORKER_REGISTRY.get_sample_value(
        "findings_posted_total", {"severity": severity}
    )
    return value or 0.0


class AcceptingPublisher:
    """The forge took every planned comment."""

    async def publish(
        self,
        target: PullRequestTarget,
        plan: PublishPlan,
        existing_check: ExistingCheckRun | None = None,
    ) -> PublishOutcome:
        return PublishOutcome(
            check_run_id=1,
            check_run_url=None,
            review_id=2,
            review_url=None,
            comments=tuple(
                PublishedComment(c.fingerprint, external_comment_id=10 + i, url=None)
                for i, c in enumerate(plan.comments)
            ),
        )


async def test_a_diff_that_does_not_parse_fails_the_context_step_not_the_fetch() -> None:
    store = MemoryJobStore()
    await enqueue(store, make_payload())
    observer = RecordingObserver()

    await process_claimed_job(
        await claim(store),
        FakeGitProvider(make_context("this is not a unified diff")),
        store,
        observer=observer,
    )

    assert [(name, outcome) for name, _, outcome in observer.steps] == [
        ("CLAIMED", "OK"),
        ("FETCH_DIFF", "OK"),
        ("BUILD_CONTEXT", "FAILED"),
    ]


async def test_a_review_counts_its_posted_findings_and_times_each_db_call() -> None:
    store = MemoryJobStore()
    await enqueue(store, make_payload())
    operations = ("extend_lease", "posted_fingerprints", "record_findings", "complete")
    before = {op: db_calls(op) for op in operations}
    high = posted("high")

    done = await process_claimed_job(
        await claim(store),
        FakeGitProvider(make_context(GOOD_DIFF)),
        store,
        review=ReviewStage(
            # Line 2 of app/users.py is an added line, so it is posted inline.
            gateway=FakeGateway((finding("app/users.py", 2),)),
            publisher=AcceptingPublisher(),
            comments=MemoryReviewCommentStore(store),
        ),
    )

    assert done is True
    assert posted("high") == high + 1
    # The lease is renewed before the LLM call and again before posting.
    assert {op: db_calls(op) - before[op] for op in operations} == {
        "extend_lease": 2,
        "posted_fingerprints": 1,
        "record_findings": 1,
        "complete": 1,
    }


async def test_a_claim_and_a_failure_are_timed_too() -> None:
    store = MemoryJobStore()
    await enqueue(store, make_payload())
    before = (db_calls("claim"), db_calls("fail"))

    await claim_and_process_once(
        store, FakeGitProvider(make_context("this is not a unified diff")), "w-1"
    )
    # Nothing left: an empty claim is a Postgres call as well.
    await claim_and_process_once(store, FakeGitProvider(make_context(GOOD_DIFF)), "w-1")

    assert (db_calls("claim"), db_calls("fail")) == (before[0] + 2, before[1] + 1)


def test_the_series_start_at_zero() -> None:
    metrics.init_worker_metrics(ERROR_KINDS, steps=[step.value for step in RECORDED_STEPS])

    registry = metrics.WORKER_REGISTRY
    for operation in metrics.DB_OPERATIONS:
        assert db_calls(operation) is not None
    for severity in ("critical", "high", "medium", "low"):
        assert (
            registry.get_sample_value("findings_posted_total", {"severity": severity}) is not None
        )
    assert (
        registry.get_sample_value(
            "review_step_duration_seconds_count", {"step": "BUILD_CONTEXT", "outcome": "OK"}
        )
        is not None
    )
