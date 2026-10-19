from domain.findings import published_findings
from domain.models import Category, DiffSide, Finding, FindingPosition, Severity
from domain.ports import PublishedComment, PublishOutcome
from domain.publishing import CommentableLines, PublishPlan, build_plan


def make_finding(line: int, message: str) -> Finding:
    return Finding(
        position=FindingPosition(path="app.py", line=line, side=DiffSide.NEW),
        severity=Severity.HIGH,
        category=Category.SECURITY,
        message=message,
        suggestion=None,
    )


def make_plan() -> PublishPlan:
    lines = CommentableLines({"app.py": {(DiffSide.NEW, 1): "a = 1", (DiffSide.NEW, 2): "b = 2"}})
    plan = build_plan(
        [make_finding(1, "First problem."), make_finding(2, "Second problem.")],
        model_summary="Two problems.",
        lines=lines,
    )
    assert len(plan.comments) == 2
    return plan


def outcome(*comments: PublishedComment, review_id: int | None = 900) -> PublishOutcome:
    return PublishOutcome(
        check_run_id=1,
        check_run_url=None,
        review_id=review_id,
        review_url=None,
        comments=comments,
    )


def test_only_comments_the_forge_reported_are_recorded_with_their_ids() -> None:
    plan = make_plan()
    first, second = plan.comments

    recorded = published_findings(plan, outcome(PublishedComment(second.fingerprint, 77, None)))

    assert len(recorded) == 1
    (item,) = recorded
    assert item.fingerprint == second.fingerprint
    assert item.finding == second.finding
    assert item.body == second.body
    assert (item.external_comment_id, item.external_review_id) == (77, 900)
    assert first.fingerprint not in {entry.fingerprint for entry in recorded}


def test_review_body_fallback_records_no_comment_id_but_the_review() -> None:
    plan = make_plan()

    recorded = published_findings(
        plan,
        outcome(*(PublishedComment(c.fingerprint, None, "u") for c in plan.comments), review_id=5),
    )

    assert [entry.fingerprint for entry in recorded] == [c.fingerprint for c in plan.comments]
    assert {(entry.external_comment_id, entry.external_review_id) for entry in recorded} == {
        (None, 5)
    }
