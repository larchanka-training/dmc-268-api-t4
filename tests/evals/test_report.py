from pathlib import Path

from domain.models import Category, DiffSide, Severity
from evals.diffinfo import changed_lines
from evals.models import Case, ExpectedDefect, Metrics, PredictedFinding
from evals.report import render_summary
from evals.runner import RunRecord
from evals.scoring import ReviewOutcome, RunStatus, score_case

DIFF = "@@ -1 +1,2 @@\n a\n+b\n"
CHANGED = changed_lines("diff --git a/f b/f\n--- a/f\n+++ b/f\n" + DIFF)


def record(case_id: str, *, tags: frozenset[str], hit: bool) -> RunRecord:
    defect = ExpectedDefect("f", 2, DiffSide.NEW, Category.CORRECTNESS, Severity.LOW)
    case = Case(
        id=case_id,
        intent="i",
        pattern="p",
        metrics=Metrics.DETECTION,
        fixture=Path("unused.diff"),
        title="t",
        description="",
        expected=(defect,),
        tags=tags,
    )
    findings = (PredictedFinding("f", 2, DiffSide.NEW, Category.CORRECTNESS, Severity.LOW),)
    outcome = ReviewOutcome(RunStatus.OK, findings if hit else ())
    return RunRecord(case=case, score=score_case(case, CHANGED, outcome))


def test_the_summary_reports_the_hold_out_cases_separately() -> None:
    records = [
        record("tuned", tags=frozenset(), hit=True),
        record("held", tags=frozenset({"holdout"}), hit=False),
    ]

    text = render_summary(records)

    assert "recall: **50.0%**" in text
    assert "hold-out (1 cases): precision **100.0%**, recall **0.0%**" in text


def test_the_summary_has_no_hold_out_line_without_hold_out_cases() -> None:
    text = render_summary([record("tuned", tags=frozenset(), hit=True)])

    assert "hold-out" not in text
