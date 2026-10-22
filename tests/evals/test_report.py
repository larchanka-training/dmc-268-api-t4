from dataclasses import replace
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


def test_the_summary_reports_volume_verifier_drops_tokens_and_the_config() -> None:
    hit = record("tuned", tags=frozenset(), hit=True)
    records = [
        replace(
            hit, findings_before_verifier=3, verifier_rejected=2, input_tokens=900, llm_calls=1
        ),
        replace(
            record("held", tags=frozenset({"holdout"}), hit=False),
            findings_before_verifier=1,
            verifier_rejected=1,
            input_tokens=700,
            llm_calls=2,
        ),
    ]

    text = render_summary(records, config="prompt v2, scope pr")

    assert "- config: `prompt v2, scope pr`" in text
    assert "findings per case: **0.50** (before the verifier 2.00)" in text
    assert "verifier dropped: **75.0%** (3 of 4)" in text
    assert "**800 per case** (1600 total over 3 LLM call(s))" in text
    assert "- tuning (1 cases): precision **100.0%**, recall **100.0%**" in text
