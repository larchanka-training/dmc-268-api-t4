from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from domain.models import Category, DiffSide, Severity


class Metrics(StrEnum):
    DETECTION = "detection"
    NEGATIVE_CONTROL = "negative control"
    SCHEMA_ONLY = "schema-only"


@dataclass(frozen=True, slots=True)
class ExpectedDefect:
    path: str
    line: int
    side: DiffSide
    category: Category
    severity: Severity

    @property
    def key(self) -> tuple[str, int, DiffSide, Category]:
        """The match key from docs/TEST_PLAN.md § Match; severity is compared separately."""
        return (self.path, self.line, self.side, self.category)


@dataclass(frozen=True, slots=True)
class CleanRegion:
    """A region no finding may touch: a whole file, or an inclusive line range of it."""

    path: str
    first_line: int | None = None
    last_line: int | None = None

    def covers(self, path: str, line: int) -> bool:
        if path != self.path:
            return False
        if self.first_line is None or self.last_line is None:
            return True
        return self.first_line <= line <= self.last_line


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    intent: str
    pattern: str
    metrics: Metrics
    fixture: Path
    title: str
    description: str
    expected: tuple[ExpectedDefect, ...]
    must_not: tuple[CleanRegion, ...] = ()
    tags: frozenset[str] = field(default_factory=frozenset)

    def read_diff(self) -> str:
        return self.fixture.read_text(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class PredictedFinding:
    """What the scorer needs of a model finding. The message is deliberately absent."""

    path: str
    line: int
    side: DiffSide
    category: Category
    severity: Severity

    @property
    def key(self) -> tuple[str, int, DiffSide, Category]:
        return (self.path, self.line, self.side, self.category)
