import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from domain.models import (
    MESSAGE_MAX_LENGTH,
    Category,
    DiffSide,
    Finding,
    FindingPosition,
    FindingSuggestion,
    RiskLevel,
    Severity,
)

# Limits from docs/schemas/llm-output.schema.json (the contract agreed in #11).
SUMMARY_MAX_LENGTH = 4000
FINDINGS_MAX = 50
PATH_MAX_LENGTH = 1024
SUGGESTION_MAX_LINES = 50
# Schema v2 (docs/schemas/llm-output.v2.schema.json): a checklist for the human reviewer.
CHECKLIST_MAX_ITEMS = 5
CHECKLIST_ITEM_MAX_LENGTH = 200
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
    rule_id: str | None = None

    @field_validator("side", "severity", "category", mode="before")
    @classmethod
    def _enum_as_lowercase(cls, value: Any) -> Any:
        # "High" or " security" is the right value in the wrong case: keep the finding.
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("rule_id", mode="before")
    @classmethod
    def _rule_id_as_text(cls, value: Any) -> Any:
        # A malformed rule_id must not cost the finding: it becomes an unknown id, which
        # parse_review then clears and counts.
        if value is None or isinstance(value, str):
            return value
        return ""

    def to_domain(self) -> Finding:
        return Finding(
            position=FindingPosition(path=self.path, line=self.line, side=self.side),
            severity=self.severity,
            category=self.category,
            message=self.message,
            suggestion=self.suggestion.to_domain() if self.suggestion else None,
            rule_id=self.rule_id,
        )


class ReviewOut(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    summary: str
    findings: list[FindingOut]
    risk: RiskLevel | None = None
    checklist: tuple[str, ...] = ()

    def to_domain(self) -> tuple[Finding, ...]:
        return tuple(finding.to_domain() for finding in self.findings)


class _ReviewEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str
    findings: list[Any]
    # v2 fields, checked one by one so a bad value costs the field, not the review.
    risk: Any = None
    checklist: Any = None


@dataclass(frozen=True, slots=True)
class ParsedReview:
    review: ReviewOut
    dropped_findings: int
    trimmed_findings: int = 0
    unknown_rule_ids: int = 0
    # Schema field that failed first, per dropped finding: names only, never values.
    dropped_fields: dict[str, int] = field(default_factory=dict)
    risk_invalid: bool = False
    checklist_invalid_items: int = 0
    checklist_trimmed: int = 0


def strip_code_fence(text: str) -> str:
    match = _FENCE.match(text)
    return match.group("body") if match else text.strip()


def _known_rule_ids(
    findings: list[FindingOut], rule_ids: frozenset[str]
) -> tuple[list[FindingOut], int]:
    """Clear rule_id values that name no rule we sent; the findings themselves stay."""
    checked: list[FindingOut] = []
    unknown = 0
    for finding in findings:
        if finding.rule_id is not None and finding.rule_id not in rule_ids:
            finding = finding.model_copy(update={"rule_id": None})
            unknown += 1
        checked.append(finding)
    return checked, unknown


def parse_review(raw_reply: str, rule_ids: frozenset[str] = frozenset()) -> ParsedReview:
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
    failed_fields: Counter[str] = Counter()
    for item in envelope.findings:
        try:
            valid.append(FindingOut.model_validate(item))
        except ValidationError as exc:
            failed_fields[_failed_field(exc)] += 1
    valid, unknown_rule_ids = _known_rule_ids(valid, rule_ids)
    kept = valid
    if len(valid) > FINDINGS_MAX:
        # Over the limit, keep the most severe: the dashboard reads them first. sorted() is
        # stable, so findings of equal severity keep the model's order.
        kept = sorted(valid, key=lambda finding: _SEVERITY_RANK[finding.severity])[:FINDINGS_MAX]
    risk, risk_invalid = _parse_risk(envelope.risk)
    checklist, invalid_items, trimmed_items = _parse_checklist(envelope.checklist)
    review = ReviewOut(
        summary=envelope.summary.strip()[:SUMMARY_MAX_LENGTH],
        findings=kept,
        risk=risk,
        checklist=checklist,
    )
    return ParsedReview(
        review=review,
        dropped_findings=len(envelope.findings) - len(valid),
        trimmed_findings=len(valid) - len(kept),
        unknown_rule_ids=unknown_rule_ids,
        dropped_fields=dict(failed_fields),
        risk_invalid=risk_invalid,
        checklist_invalid_items=invalid_items,
        checklist_trimmed=trimmed_items,
    )


def _parse_risk(value: Any) -> tuple[RiskLevel | None, bool]:
    """The risk and whether a value was given but unusable; absent is not invalid (v1)."""
    if value is None:
        return None, False
    if isinstance(value, str):
        try:
            return RiskLevel(value.strip().lower()), False
        except ValueError:
            pass
    return None, True


def _parse_checklist(value: Any) -> tuple[tuple[str, ...], int, int]:
    """Items as one-line strings, at most CHECKLIST_MAX_ITEMS; plus invalid and cut counts."""
    if value is None:
        return (), 0, 0
    if not isinstance(value, list):
        return (), 1, 0
    items: list[str] = []
    invalid = 0
    for item in value:
        text = " ".join(item.split())[:CHECKLIST_ITEM_MAX_LENGTH] if isinstance(item, str) else ""
        if text:
            items.append(text)
        else:
            invalid += 1
    return tuple(items[:CHECKLIST_MAX_ITEMS]), invalid, max(len(items) - CHECKLIST_MAX_ITEMS, 0)


def _failed_field(error: ValidationError) -> str:
    location = error.errors()[0]["loc"]
    return str(location[0]) if location else "finding"
