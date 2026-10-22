from dataclasses import dataclass
from enum import StrEnum

MESSAGE_MAX_LENGTH = 2000


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Category(StrEnum):
    SECURITY = "security"
    CORRECTNESS = "correctness"
    CONCURRENCY = "concurrency"
    PERFORMANCE = "performance"
    MAINTAINABILITY = "maintainability"


class RiskLevel(StrEnum):
    """Release risk of a whole pull request, for the human reviewer (issue #55)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Lowest first, so max(..., key=RISK_ORDER.index) picks the higher risk.
RISK_ORDER: tuple[RiskLevel, ...] = (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH)


class DiffSide(StrEnum):
    OLD = "old"
    NEW = "new"


class VerificationOutcome(StrEnum):
    """What the second, verifying pass did with the first pass's findings."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class FindingPosition:
    path: str
    line: int
    side: DiffSide

    def __post_init__(self) -> None:
        if not self.path.strip():
            raise ValueError("finding path must not be empty")
        if self.line < 1:
            raise ValueError(f"finding line must be >= 1, got {self.line}")


@dataclass(frozen=True, slots=True)
class FindingSuggestion:
    before: tuple[str, ...]
    after: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Finding:
    position: FindingPosition
    severity: Severity
    category: Category
    message: str
    suggestion: FindingSuggestion | None
    # Id of the repository rule (R1, R2, ...) the finding violates, if any.
    rule_id: str | None = None
    # Probability from the verifier that the finding is real; None when it did not run.
    verifier_score: float | None = None

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("finding message must not be empty")
        if len(self.message) > MESSAGE_MAX_LENGTH:
            raise ValueError(f"finding message exceeds {MESSAGE_MAX_LENGTH} characters")


@dataclass(frozen=True, slots=True)
class ReviewResult:
    summary: str
    findings: tuple[Finding, ...]
    provider: str
    model: str
    verification: VerificationOutcome = VerificationOutcome.DISABLED
    rejected_by_verifier: int = 0
    # The model's own assessment (prompt v2). None when it gave none or an invalid one;
    # publishing then falls back to the risk the findings imply.
    risk: RiskLevel | None = None
    checklist: tuple[str, ...] = ()
