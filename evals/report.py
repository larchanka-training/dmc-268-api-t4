"""Render results. Finding keys and counts only: never a message, a suggestion or a reply."""

from collections.abc import Sequence
from typing import Any

from evals.runner import RunRecord
from evals.scoring import CorpusScore, aggregate, near_misses

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


def _per_case(total: int, runs: int) -> str:
    return "n/a" if runs == 0 else f"{total / runs:.2f}"


def _volume_lines(records: Sequence[RunRecord]) -> list[str]:
    """How much the model said and what it cost: findings, verifier drops, input tokens."""
    runs = len(records)
    kept = sum(len(record.score.findings) for record in records)
    before = sum(record.findings_before_verifier for record in records)
    rejected = sum(record.verifier_rejected for record in records)
    tokens = sum(record.input_tokens for record in records)
    calls = sum(record.llm_calls for record in records)
    share = "n/a" if before == 0 else _percent(rejected / before)
    misses = sum(near_misses(record.case, record.score) for record in records)
    return [
        f"- findings per case: **{_per_case(kept, runs)}** "
        f"(before the verifier {_per_case(before, runs)})",
        f"- verifier dropped: **{share}** ({rejected} of {before})",
        f"- input tokens (estimated, chars/3): **{_per_case(tokens, runs)} per case** "
        f"({tokens} total over {calls} LLM call(s))",
        f"- near misses (right defect, wrong line or category): {misses}",
    ]


def render_summary(records: Sequence[RunRecord], *, config: str | None = None) -> str:
    corpus = corpus_score(records)
    holdout = [r for r in records if HOLDOUT_TAG in r.case.tags]
    tuning = [r for r in records if HOLDOUT_TAG not in r.case.tags]
    served = sorted({(r.provider, r.model) for r in records if r.provider and r.model})
    lines = ["## LLM eval", ""]
    if config:
        lines.append(f"- config: `{config}`")
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
        *_volume_lines(records),
    ]
    if holdout and tuning:
        tuned = corpus_score(tuning)
        lines.append(
            f"- tuning ({tuned.runs} cases): precision **{_percent(tuned.precision)}**, "
            f"recall **{_percent(tuned.recall)}** (TP {tuned.tp}, FP {tuned.fp}, FN {tuned.fn})"
        )
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
        "| case | metrics | run | TP | FP | FN | halluc. | sev. err | dropped | near miss "
        "| findings | verifier dropped | input tokens | prec. | recall |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
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
                    near_misses(record.case, score),
                    len(score.findings),
                    record.verifier_rejected,
                    record.input_tokens,
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
            "findings_before_verifier": sum(r.findings_before_verifier for r in records),
            "verifier_rejected": sum(r.verifier_rejected for r in records),
            "input_tokens": sum(r.input_tokens for r in records),
            "llm_calls": sum(r.llm_calls for r in records),
            "near_misses": sum(near_misses(r.case, r.score) for r in records),
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
                "findings_before_verifier": record.findings_before_verifier,
                "verifier_rejected": record.verifier_rejected,
                "input_tokens": record.input_tokens,
                "llm_calls": record.llm_calls,
                "near_misses": near_misses(record.case, record.score),
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
