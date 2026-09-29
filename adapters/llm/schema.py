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

_FENCE = re.compile(r"\A\s*```[A-Za-z]*[ \t]*\n(?P<body>.*?)\n?[ \t]*```\s*\Z", re.DOTALL)

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
FindingMessage = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MESSAGE_MAX_LENGTH)
]


class InvalidReviewJSONError(ValueError):
    """The model reply is not a JSON review object at all; worth one corrective retry."""


class SuggestionOut(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    before: list[str]
    after: list[str]

    def to_domain(self) -> FindingSuggestion:
        return FindingSuggestion(before=tuple(self.before), after=tuple(self.after))


class FindingOut(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    path: NonEmptyText
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
    review = ReviewOut(summary=envelope.summary.strip(), findings=valid)
    return ParsedReview(review=review, dropped_findings=len(envelope.findings) - len(valid))
