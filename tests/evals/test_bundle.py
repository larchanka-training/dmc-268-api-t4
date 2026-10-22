"""The bundle case and the near-miss count of the eval report."""

from pathlib import Path

from domain.models import Category, DiffSide, Severity
from evals.bundle import BUNDLE_ID, build_bundle, prefix_paths
from evals.diffinfo import changed_lines
from evals.loader import load_dataset
from evals.models import Case, ExpectedDefect, Metrics, PredictedFinding
from evals.scoring import ReviewOutcome, RunStatus, near_misses, score_case

DATASET = Path(__file__).resolve().parents[2] / "test-prs-dataset"
DIFF = (
    "diff --git a/app/x.py b/app/y.py\nsimilarity index 90%\nrename from app/x.py\n"
    "rename to app/y.py\n--- a/app/x.py\n+++ b/app/y.py\n@@ -1 +1,2 @@\n a\n"
    "+--- a/not/a/header\n"
)


def test_prefix_paths_rewrites_headers_and_leaves_hunk_bodies_alone() -> None:
    out = prefix_paths(DIFF, "case-1")

    assert "diff --git a/case-1/app/x.py b/case-1/app/y.py" in out
    assert "rename from case-1/app/x.py" in out
    assert "rename to case-1/app/y.py" in out
    assert "--- a/case-1/app/x.py\n+++ b/case-1/app/y.py" in out
    assert out.endswith("+--- a/not/a/header\n")


def test_the_bundle_keeps_every_expected_defect_on_a_changed_line() -> None:
    cases = load_dataset(DATASET)

    bundle, diff_text = build_bundle(cases, Path("bundle.diff"))

    changed = changed_lines(diff_text)
    scored = [case for case in cases if case.metrics is not Metrics.SCHEMA_ONLY]
    assert bundle.id == BUNDLE_ID
    assert bundle.metrics is Metrics.DETECTION
    assert len(bundle.expected) == sum(len(case.expected) for case in scored)
    assert len({defect.key for defect in bundle.expected}) == len(bundle.expected)
    assert all(changed.contains(d.path, d.line, d.side) for d in bundle.expected)
    assert all(region.path in changed.paths for region in bundle.must_not)
    assert len(changed.paths) >= len(scored)


def _case(*expected: ExpectedDefect) -> Case:
    return Case(
        id="c",
        intent="i",
        pattern="p",
        metrics=Metrics.DETECTION,
        fixture=Path("unused.diff"),
        title="t",
        description="",
        expected=expected,
    )


def test_a_finding_next_to_a_missed_defect_is_a_near_miss() -> None:
    defect = ExpectedDefect("f", 5, DiffSide.NEW, Category.CONCURRENCY, Severity.HIGH)
    case = _case(defect)
    changed = changed_lines("diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,0 +1,9 @@\n" + "+x\n" * 9)
    findings = (
        PredictedFinding("f", 6, DiffSide.NEW, Category.CONCURRENCY, Severity.HIGH),
        PredictedFinding("f", 5, DiffSide.NEW, Category.CORRECTNESS, Severity.HIGH),
        PredictedFinding("f", 9, DiffSide.NEW, Category.CONCURRENCY, Severity.HIGH),
    )

    score = score_case(case, changed, ReviewOutcome(RunStatus.OK, findings))

    assert (score.tp, score.fp, score.fn) == (0, 3, 1)
    assert near_misses(case, score) == 1
