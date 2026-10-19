from pathlib import Path

from domain.models import Category, DiffSide, Severity
from evals.diffinfo import changed_lines
from evals.models import Case, CleanRegion, ExpectedDefect, Metrics, PredictedFinding
from evals.scoring import ReviewOutcome, RunStatus, Verdict, aggregate, score_case

DIFF = """\
diff --git a/app/users.py b/app/users.py
index 3f2a1c4..9b7e0d2 100644
--- a/app/users.py
+++ b/app/users.py
@@ -1,4 +1,6 @@
 import sqlite3
+import os
+
 
 DB = "app.db"
 
"""


def defect(line: int = 2, severity: Severity = Severity.HIGH) -> ExpectedDefect:
    return ExpectedDefect("app/users.py", line, DiffSide.NEW, Category.SECURITY, severity)


def found(
    line: int = 2,
    *,
    path: str = "app/users.py",
    category: Category = Category.SECURITY,
    severity: Severity = Severity.HIGH,
) -> PredictedFinding:
    return PredictedFinding(path, line, DiffSide.NEW, category, severity)


def make_case(
    metrics: Metrics = Metrics.DETECTION,
    expected: tuple[ExpectedDefect, ...] | None = None,
    must_not: tuple[CleanRegion, ...] = (),
) -> Case:
    if expected is None:
        expected = (defect(),) if metrics is Metrics.DETECTION else ()
    return Case(
        id="c",
        intent="i",
        pattern="p",
        metrics=metrics,
        fixture=Path("unused.diff"),
        title="t",
        description="",
        expected=expected,
        must_not=must_not,
    )


def ok(*findings: PredictedFinding, dropped: int = 0) -> ReviewOutcome:
    return ReviewOutcome(RunStatus.OK, findings, dropped)


CHANGED = changed_lines(DIFF)


def test_a_finding_on_the_expected_key_is_a_true_positive() -> None:
    score = score_case(make_case(), CHANGED, ok(found()))

    assert (score.tp, score.fp, score.fn) == (1, 0, 0)
    assert score.precision == 1.0
    assert score.recall == 1.0
    assert score.findings[0].verdict is Verdict.TP


def test_wrong_category_misses_the_key() -> None:
    score = score_case(make_case(), CHANGED, ok(found(category=Category.CORRECTNESS)))

    assert (score.tp, score.fp, score.fn) == (0, 1, 1)
    assert score.findings[0].verdict is Verdict.FP


def test_severity_mismatch_is_still_a_true_positive_and_a_severity_error() -> None:
    score = score_case(make_case(), CHANGED, ok(found(severity=Severity.LOW)))

    assert (score.tp, score.fp, score.fn) == (1, 0, 0)
    assert score.severity_errors == 1


def test_an_unmatched_expected_defect_is_a_false_negative() -> None:
    score = score_case(make_case(), CHANGED, ok())

    assert (score.tp, score.fp, score.fn) == (0, 0, 1)
    assert score.precision == 1.0  # TP + FP == 0
    assert score.recall == 0.0


def test_a_duplicate_finding_matches_once_and_the_copy_is_a_false_positive() -> None:
    score = score_case(make_case(), CHANGED, ok(found(), found()))

    assert (score.tp, score.fp, score.fn) == (1, 1, 0)


def test_extra_finding_on_a_changed_line_is_a_false_positive_not_a_hallucination() -> None:
    score = score_case(make_case(), CHANGED, ok(found(), found(3, category=Category.CORRECTNESS)))

    assert (score.tp, score.fp, score.hallucinations) == (1, 1, 0)
    assert score.findings[1].verdict is Verdict.FP


def test_a_finding_on_a_context_line_is_a_hallucination() -> None:
    score = score_case(make_case(), CHANGED, ok(found(), found(1)))

    assert (score.fp, score.hallucinations) == (1, 1)
    assert score.findings[1].verdict is Verdict.HALLUCINATION


def test_a_finding_on_another_path_is_a_hallucination() -> None:
    score = score_case(make_case(), CHANGED, ok(found(), found(path="app/other.py")))

    assert score.hallucinations == 1


def test_a_finding_in_a_must_not_region_is_a_hallucination_even_on_a_changed_line() -> None:
    region = CleanRegion("app/users.py", 3, 3)
    score = score_case(
        make_case(must_not=(region,)), CHANGED, ok(found(), found(3, category=Category.CORRECTNESS))
    )

    assert score.hallucinations == 1


def test_negative_control_scores_precision_and_hallucinations_only() -> None:
    clean = score_case(make_case(Metrics.NEGATIVE_CONTROL), CHANGED, ok())
    noisy = score_case(make_case(Metrics.NEGATIVE_CONTROL), CHANGED, ok(found(1)))

    assert clean.recall == 1.0
    assert clean.precision == 1.0
    assert noisy.precision == 0.0
    assert noisy.recall is None
    assert noisy.hallucinations == 1


def test_schema_only_case_scores_nothing_but_counts_drops() -> None:
    score = score_case(make_case(Metrics.SCHEMA_ONLY), CHANGED, ok(found(), dropped=3))

    assert (score.tp, score.fp, score.fn, score.hallucinations) == (0, 0, 0, 0)
    assert score.dropped == 3
    assert score.precision is None
    assert score.recall is None
    assert score.findings[0].verdict is Verdict.IGNORED


def test_a_failed_run_contributes_its_defects_as_false_negatives() -> None:
    for status in (RunStatus.SCHEMA_INVALID, RunStatus.ERRORED):
        score = score_case(make_case(), CHANGED, ReviewOutcome(status))

        assert (score.tp, score.fp, score.fn) == (0, 0, 1)
        assert score.status is status


def test_corpus_rates_follow_the_plan() -> None:
    scores = [
        score_case(make_case(), CHANGED, ok(found())),  # TP
        score_case(make_case(), CHANGED, ok(found(3, category=Category.CORRECTNESS))),  # FP+FN
        score_case(make_case(Metrics.NEGATIVE_CONTROL), CHANGED, ok(found(1))),  # FP, halluc.
        score_case(make_case(Metrics.SCHEMA_ONLY), CHANGED, ok(found(), dropped=2)),
        score_case(make_case(), CHANGED, ReviewOutcome(RunStatus.SCHEMA_INVALID)),  # FN
        score_case(make_case(), CHANGED, ReviewOutcome(RunStatus.ERRORED)),  # FN
    ]

    corpus = aggregate(scores)

    assert corpus.runs == 6
    assert corpus.schema_valid == 4
    assert (corpus.schema_invalid, corpus.errored) == (1, 1)
    assert corpus.schema_valid_rate == 4 / 6
    assert (corpus.tp, corpus.fp, corpus.fn) == (1, 2, 3)
    assert corpus.precision == 1 / 3
    assert corpus.recall == 1 / 4
    assert corpus.hallucinations == 1
    assert corpus.dropped == 2


def test_an_empty_corpus_reports_perfect_rates_rather_than_dividing_by_zero() -> None:
    corpus = aggregate([])

    assert (corpus.schema_valid_rate, corpus.precision, corpus.recall) == (1.0, 1.0, 1.0)
