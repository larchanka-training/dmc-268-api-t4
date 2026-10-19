from dataclasses import dataclass

from domain.diff import DiffLineKind, parse_diff
from domain.models import DiffSide


@dataclass(frozen=True, slots=True)
class ChangedLines:
    """Where a diff changes code, as the (path, side) -> line numbers a finding may anchor to."""

    paths: frozenset[str]
    lines: dict[tuple[str, DiffSide], frozenset[int]]

    def contains(self, path: str, line: int, side: DiffSide) -> bool:
        return line in self.lines.get((path, side), frozenset())


def changed_lines(diff_text: str) -> ChangedLines:
    """Raise domain.errors.DiffFormatError when the text is not a unified git diff."""
    paths: set[str] = set()
    collected: dict[tuple[str, DiffSide], set[int]] = {}
    for file in parse_diff(diff_text):
        path = file.new_path or file.old_path
        paths.add(path)
        for hunk in file.hunks:
            for line in hunk.lines:
                if line.kind is DiffLineKind.ADDED and line.new_lineno is not None:
                    collected.setdefault((path, DiffSide.NEW), set()).add(line.new_lineno)
                elif line.kind is DiffLineKind.DELETED and line.old_lineno is not None:
                    collected.setdefault((path, DiffSide.OLD), set()).add(line.old_lineno)
    return ChangedLines(
        paths=frozenset(paths),
        lines={key: frozenset(value) for key, value in collected.items()},
    )
