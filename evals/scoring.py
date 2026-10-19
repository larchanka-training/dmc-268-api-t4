"""Score findings against a case. Definitions: docs/TEST_PLAN.md § LLM quality method.

Pure functions over keys (path, line, side, category, severity). Message text never reaches
this module (AGENTS.md hard rule 3).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from evals.diffinfo import ChangedLines
from evals.models import Case, Metrics, PredictedFinding


class RunStatus(StrEnum):
    OK = "ok"
    SCHEMA_INVALID = "schema-invalid"  # the reply never parsed, even after the corrective retry
    ERRORED = "errored"  # infrastructure: timeout, 5xx, exhausted keys, oversized input


class Verdict(StrEnum):
    TP = "tp"
    FP = "fp"
    HALLUCINATION = "hallucination"  # a false positive off the changed lines or inside must_not
    IGNORED = "ignored"  # a finding of a schema-only case: it scores nothing


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    status: RunStatus
    findings: tuple[PredictedFinding, ...] = ()
    dropped: int = 0


@dataclass(frozen=True, slots=True)
class ScoredFinding:
    finding: PredictedFinding
    verdict: Verdict


@dataclass(frozen=True, slots=True)
class CaseScore:
    case_id: str
    metrics: Metrics
    status: RunStatus
    tp: int
    fp: int
    fn: int
    hallucinations: int
    severity_errors: int
    dropped: int
    findings: tuple[ScoredFinding, ...]

    @property
    def precision(self) -> float | None:
        if self.metrics is Metrics.SCHEMA_ONLY:
            return None
        return _ratio(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float | None:
        if self.metrics is Metrics.SCHEMA_ONLY:
            return None
        if self.metrics is Metrics.NEGATIVE_CONTROL:
            return 1.0 if self.fp == 0 else None
        return _ratio(self.tp, self.tp + self.fn)


@dataclass(frozen=True, slots=True)
class CorpusScore:
    runs: int
    schema_valid: int
    schema_invalid: int
    errored: int
    tp: int
    fp: int
    fn: int
    hallucinations: int
    severity_errors: int
    dropped: int

    @property
    def schema_valid_rate(self) -> float:
        return _ratio(self.schema_valid, self.runs)

    @property
    def precision(self) -> float:
        return _ratio(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float:
        return _ratio(self.tp, self.tp + self.fn)


def _ratio(numerator: int, denominator: int) -> float:
    return 1.0 if denominator == 0 else numerator / denominator


def score_case(case: Case, changed: ChangedLines, outcome: ReviewOutcome) -> CaseScore:
    detection = case.metrics is Metrics.DETECTION
    if outcome.status is not RunStatus.OK:
        return CaseScore(
            case_id=case.id,
            metrics=case.metrics,
            status=outcome.status,
            tp=0,
            fp=0,
            fn=len(case.expected),
            hallucinations=0,
            severity_errors=0,
            dropped=0,
            findings=(),
        )

    if case.metrics is Metrics.SCHEMA_ONLY:
        return CaseScore(
            case_id=case.id,
            metrics=case.metrics,
            status=outcome.status,
            tp=0,
            fp=0,
            fn=0,
            hallucinations=0,
            severity_errors=0,
            dropped=outcome.dropped,
            findings=tuple(ScoredFinding(f, Verdict.IGNORED) for f in outcome.findings),
        )

    open_defects = {defect.key: defect for defect in case.expected}
    scored: list[ScoredFinding] = []
    tp = fp = hallucinations = severity_errors = 0
    for finding in outcome.findings:
        defect = open_defects.pop(finding.key, None)
        if defect is not None:
            tp += 1
            if defect.severity is not finding.severity:
                severity_errors += 1
            scored.append(ScoredFinding(finding, Verdict.TP))
            continue
        fp += 1
        off_hunk = not changed.contains(finding.path, finding.line, finding.side)
        in_clean = any(region.covers(finding.path, finding.line) for region in case.must_not)
        if off_hunk or in_clean:
            hallucinations += 1
            scored.append(ScoredFinding(finding, Verdict.HALLUCINATION))
        else:
            scored.append(ScoredFinding(finding, Verdict.FP))
    return CaseScore(
        case_id=case.id,
        metrics=case.metrics,
        status=outcome.status,
        tp=tp,
        fp=fp,
        fn=len(open_defects) if detection else 0,
        hallucinations=hallucinations,
        severity_errors=severity_errors,
        dropped=outcome.dropped,
        findings=tuple(scored),
    )


def aggregate(scores: Iterable[CaseScore]) -> CorpusScore:
    """Corpus precision sums TP and FP over detection cases and negative controls.

    Corpus recall sums TP and FN over detection cases only. Schema-only cases feed the
    schema-valid rate and the dropped-finding count and nothing else.
    """
    all_scores: Sequence[CaseScore] = list(scores)
    detection = [s for s in all_scores if s.metrics is Metrics.DETECTION]
    scored = [s for s in all_scores if s.metrics is not Metrics.SCHEMA_ONLY]
    return CorpusScore(
        runs=len(all_scores),
        schema_valid=sum(s.status is RunStatus.OK for s in all_scores),
        schema_invalid=sum(s.status is RunStatus.SCHEMA_INVALID for s in all_scores),
        errored=sum(s.status is RunStatus.ERRORED for s in all_scores),
        tp=sum(s.tp for s in detection),
        fp=sum(s.fp for s in scored),
        fn=sum(s.fn for s in detection),
        hallucinations=sum(s.hallucinations for s in scored),
        severity_errors=sum(s.severity_errors for s in scored),
        dropped=sum(s.dropped for s in all_scores),
    )
