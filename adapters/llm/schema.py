import json
import re
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from domain.models import (
    MESSAGE_MAX_LENGTH,
    Category,
    DiffSide,
    Finding,
    FindingPosition,
    FindingSuggestion,
    Severity,
)

# Limits from docs/schemas/llm-output.schema.json (the contract agreed in #11).
SUMMARY_MAX_LENGTH = 4000
FINDINGS_MAX = 50
PATH_MAX_LENGTH = 1024
SUGGESTION_MAX_LINES = 50
_SEVERITY_RANK = {severity: rank for rank, severity in enumerate(Severity)}

_FENCE = re.compile(r"\A\s*```[A-Za-z]*[ \t]*\n(?P<body>.*?)\n?[ \t]*```\s*\Z", re.DOTALL)

FindingPath = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=PATH_MAX_LENGTH)
]
FindingMessage = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MESSAGE_MAX_LENGTH)
]


class InvalidReviewJSONError(ValueError):
    """The model reply is not a JSON review object at all; worth one corrective retry."""


class SuggestionOut(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    before: list[str] = Field(max_length=SUGGESTION_MAX_LINES)
    after: list[str] = Field(max_length=SUGGESTION_MAX_LINES)

    def to_domain(self) -> FindingSuggestion:
        return FindingSuggestion(before=tuple(self.before), after=tuple(self.after))


class FindingOut(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    path: FindingPath
    line: int = Field(ge=1, strict=True)
    side: DiffSide
    severity: Severity
    category: Category
    message: FindingMessage
    suggestion: SuggestionOut | None = None

    def to_domain(self) -> Finding:
        return Finding(
            position=FindingPosition(path=self.path, line=self.line, side=self.side),
            severity=self.severity,
            category=self.category,
            message=self.message,
            suggestion=self.suggestion.to_domain() if self.suggestion else None,
        )


class ReviewOut(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    summary: str
    findings: list[FindingOut]

    def to_domain(self) -> tuple[Finding, ...]:
        return tuple(finding.to_domain() for finding in self.findings)


class _ReviewEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str
    findings: list[Any]


@dataclass(frozen=True, slots=True)
class ParsedReview:
    review: ReviewOut
    dropped_findings: int
    trimmed_findings: int = 0


def strip_code_fence(text: str) -> str:
    match = _FENCE.match(text)
    return match.group("body") if match else text.strip()


def parse_review(raw_reply: str) -> ParsedReview:
    """Validate a model reply; drop findings that break the schema instead of failing."""
    try:
        payload = json.loads(strip_code_fence(raw_reply))
    except json.JSONDecodeError as exc:
        raise InvalidReviewJSONError(f"reply is not valid JSON ({exc.msg})") from None
    try:
        envelope = _ReviewEnvelope.model_validate(payload)
    except ValidationError as exc:
        raise InvalidReviewJSONError(
            f"reply does not match the review schema ({exc.error_count()} error(s))"
        ) from None

    valid: list[FindingOut] = []
    for item in envelope.findings:
        try:
            valid.append(FindingOut.model_validate(item))
        except ValidationError:
            continue
    kept = valid
    if len(valid) > FINDINGS_MAX:
        # Over the limit, keep the most severe: the dashboard reads them first. sorted() is
        # stable, so findings of equal severity keep the model's order.
        kept = sorted(valid, key=lambda finding: _SEVERITY_RANK[finding.severity])[:FINDINGS_MAX]
    review = ReviewOut(summary=envelope.summary.strip()[:SUMMARY_MAX_LENGTH], findings=kept)
    return ParsedReview(
        review=review,
        dropped_findings=len(envelope.findings) - len(valid),
        trimmed_findings=len(valid) - len(kept),
    )
