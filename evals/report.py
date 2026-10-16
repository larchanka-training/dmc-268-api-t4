"""Render results. Finding keys and counts only: never a message, a suggestion or a reply."""

from collections.abc import Sequence
from typing import Any

from evals.runner import RunRecord
from evals.scoring import CorpusScore, aggregate

HOLDOUT_TAG = "holdout"
RUN_LIMIT_NOTE = (
    "One run per case at temperature 0.0. An identical prompt can still yield different "
    "findings, so read small differences between two runs as noise."
)


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|")


def corpus_score(records: Sequence[RunRecord]) -> CorpusScore:
    return aggregate(record.score for record in records)


def render_summary(records: Sequence[RunRecord]) -> str:
    corpus = corpus_score(records)
    holdout = [r for r in records if HOLDOUT_TAG in r.case.tags]
    served = sorted({(r.provider, r.model) for r in records if r.provider and r.model})
    lines = ["## LLM eval", ""]
    lines.extend(f"- provider `{provider}`, model `{model}`" for provider, model in served)
    lines += [
        f"- cases: **{corpus.runs}**",
        f"- schema-valid: **{_percent(corpus.schema_valid_rate)}** "
        f"({corpus.schema_valid}/{corpus.runs}; schema-invalid {corpus.schema_invalid}, "
        f"errored {corpus.errored})",
        f"- precision: **{_percent(corpus.precision)}** "
        f"(TP {corpus.tp}, FP {corpus.fp}, hallucinations {corpus.hallucinations})",
        f"- recall: **{_percent(corpus.recall)}** (TP {corpus.tp}, FN {corpus.fn})",
        f"- severity errors: {corpus.severity_errors}",
        f"- dropped findings: {corpus.dropped}",
    ]
    if holdout:
        held = corpus_score(holdout)
        lines.append(
            f"- hold-out ({held.runs} cases): precision **{_percent(held.precision)}**, "
            f"recall **{_percent(held.recall)}** (TP {held.tp}, FP {held.fp}, FN {held.fn})"
        )
    lines += [
        "",
        f"_{RUN_LIMIT_NOTE}_",
        "",
        "| case | metrics | run | TP | FP | FN | halluc. | sev. err | dropped | prec. | recall |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for record in records:
        score = record.score
        lines.append(
            "| "
            + " | ".join(
                _cell(value)
                for value in (
                    score.case_id,
                    score.metrics.value,
                    score.status.value,
                    score.tp,
                    score.fp,
                    score.fn,
                    score.hallucinations,
                    score.severity_errors,
                    score.dropped,
                    _percent(score.precision),
                    _percent(score.recall),
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def results_json(records: Sequence[RunRecord]) -> dict[str, Any]:
    corpus = corpus_score(records)
    return {
        "corpus": {
            "runs": corpus.runs,
            "schema_valid_rate": corpus.schema_valid_rate,
            "schema_invalid": corpus.schema_invalid,
            "errored": corpus.errored,
            "precision": corpus.precision,
            "recall": corpus.recall,
            "tp": corpus.tp,
            "fp": corpus.fp,
            "fn": corpus.fn,
            "hallucinations": corpus.hallucinations,
            "severity_errors": corpus.severity_errors,
            "dropped": corpus.dropped,
        },
        "cases": [
            {
                "id": record.score.case_id,
                "metrics": record.score.metrics.value,
                "run": record.score.status.value,
                "error": record.error,
                "tp": record.score.tp,
                "fp": record.score.fp,
                "fn": record.score.fn,
                "hallucinations": record.score.hallucinations,
                "severity_errors": record.score.severity_errors,
                "dropped": record.score.dropped,
                "findings": [
                    {
                        "path": scored.finding.path,
                        "line": scored.finding.line,
                        "side": scored.finding.side.value,
                        "category": scored.finding.category.value,
                        "severity": scored.finding.severity.value,
                        "verdict": scored.verdict.value,
                    }
                    for scored in record.score.findings
                ],
            }
            for record in records
        ],
    }
