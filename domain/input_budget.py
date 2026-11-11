"""Which files to leave out when a diff does not fit the LLM input budget (issue #88).

Pure: paths and sizes in, paths out. The gateway supplies the cost of each file in the
units of its own estimate and applies the result.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

TEST_DIRECTORIES: frozenset[str] = frozenset(
    {"test", "tests", "__tests__", "spec", "specs", "testdata", "e2e"}
)
TEST_BASENAME_PREFIXES: tuple[str, ...] = ("test_",)
TEST_BASENAMES: frozenset[str] = frozenset({"conftest.py"})
# Matched against the name without its last extension: user_test.go, api.spec.ts, Button.test.tsx.
TEST_STEM_SUFFIXES: tuple[str, ...] = ("_test", ".test", ".spec", "_spec")
DOC_SUFFIXES: tuple[str, ...] = (".md", ".rst")

# Dropped first to last: a review without the tests or the docs still covers the change,
# a review without the code does not.
_TESTS, _DOCS, _CODE = range(3)


@dataclass(frozen=True, slots=True)
class FileCost:
    """A file and what it adds to the review input, in the caller's unit."""

    path: str
    cost: int


@dataclass(frozen=True, slots=True)
class BudgetFit:
    """Both in the order the files came in."""

    kept: tuple[str, ...]
    dropped: tuple[str, ...]


def is_test_path(path: str) -> bool:
    pure = PurePosixPath(path)
    name = pure.name.lower()
    if TEST_DIRECTORIES.intersection(part.lower() for part in pure.parts[:-1]):
        return True
    if name in TEST_BASENAMES or name.startswith(TEST_BASENAME_PREFIXES):
        return True
    return PurePosixPath(name).stem.endswith(TEST_STEM_SUFFIXES)


def is_doc_path(path: str) -> bool:
    return path.lower().endswith(DOC_SUFFIXES)


def _drop_rank(path: str) -> int:
    if is_test_path(path):
        return _TESTS
    if is_doc_path(path):
        return _DOCS
    return _CODE


def fit_to_budget(files: Sequence[FileCost], *, budget: int) -> BudgetFit:
    """Drop whole files until the rest costs at most `budget`.

    Tests go first, then docs (`*.md`, `*.rst`), then the remaining files; within each
    group the largest goes first, so the fewest files are lost. Nothing is dropped when
    everything fits; everything is dropped when no file does.
    """
    total = sum(file.cost for file in files)
    order = sorted(
        range(len(files)), key=lambda index: (_drop_rank(files[index].path), -files[index].cost)
    )
    dropped: set[int] = set()
    for index in order:
        if total <= budget:
            break
        dropped.add(index)
        total -= files[index].cost
    return BudgetFit(
        kept=tuple(file.path for index, file in enumerate(files) if index not in dropped),
        dropped=tuple(file.path for index, file in enumerate(files) if index in dropped),
    )
