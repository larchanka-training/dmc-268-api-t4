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


class DiffSide(StrEnum):
    OLD = "old"
    NEW = "new"


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
