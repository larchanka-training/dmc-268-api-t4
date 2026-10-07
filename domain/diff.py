"""Parse and chunk unified git diffs.

Pure text processing: stdlib only, no I/O. Error messages carry a reason and
a line number, never diff content (AGENTS.md hard rule 3).
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum

from domain.errors import DiffFormatError


class DiffLineKind(StrEnum):
    CONTEXT = "context"
    ADDED = "added"
    DELETED = "deleted"


class FileStatus(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


@dataclass(frozen=True, slots=True)
class DiffLine:
    """One line of a hunk.

    repr is suppressed on content so a traceback or a log line that formats
    this object cannot leak diff text.
    """

    kind: DiffLineKind
    old_lineno: int | None
    new_lineno: int | None
    content: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.kind is DiffLineKind.ADDED:
            if self.old_lineno is not None or self.new_lineno is None:
                raise ValueError("added line must set new_lineno only")
        elif self.kind is DiffLineKind.DELETED:
            if self.old_lineno is None or self.new_lineno is not None:
                raise ValueError("deleted line must set old_lineno only")
        elif self.old_lineno is None or self.new_lineno is None:
            raise ValueError("context line must set both old_lineno and new_lineno")
        for name, lineno in (("old_lineno", self.old_lineno), ("new_lineno", self.new_lineno)):
            if lineno is not None and lineno < 1:
                raise ValueError(f"{name} must be >= 1, got {lineno}")
        if "\n" in self.content:
            raise ValueError("line content must not contain a newline")


@dataclass(frozen=True, slots=True)
class DiffHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str | None
    lines: tuple[DiffLine, ...]

    def __post_init__(self) -> None:
        for name, start, count in (
            ("old", self.old_start, self.old_count),
            ("new", self.new_start, self.new_count),
        ):
            if count < 0:
                raise ValueError(f"{name}_count must be >= 0, got {count}")
            if start < 1 and not (start == 0 and count == 0):
                raise ValueError(f"{name}_start must be >= 1, or 0 with {name}_count 0")
        old_actual = sum(line.kind is not DiffLineKind.ADDED for line in self.lines)
        new_actual = sum(line.kind is not DiffLineKind.DELETED for line in self.lines)
        if old_actual != self.old_count:
            raise ValueError(f"old_count is {self.old_count} but lines supply {old_actual}")
        if new_actual != self.new_count:
            raise ValueError(f"new_count is {self.new_count} but lines supply {new_actual}")


@dataclass(frozen=True, slots=True)
class FileDiff:
    """One file's diff. The missing side of an added or deleted file is the empty string."""

    old_path: str
    new_path: str
    status: FileStatus
    is_binary: bool
    hunks: tuple[DiffHunk, ...]

    def __post_init__(self) -> None:
        if self.status is FileStatus.ADDED:
            if self.old_path:
                raise ValueError("an added file must have an empty old_path")
            if not self.new_path:
                raise ValueError("an added file must have a new_path")
        elif self.status is FileStatus.DELETED:
            if self.new_path:
                raise ValueError("a deleted file must have an empty new_path")
            if not self.old_path:
                raise ValueError("a deleted file must have an old_path")
        elif not self.old_path or not self.new_path:
            raise ValueError(f"a {self.status.value} file must have both old_path and new_path")


@dataclass(frozen=True, slots=True)
class Chunk:
    hunks: tuple[DiffHunk, ...]
    old_start: int
    old_end: int
    new_start: int
    new_end: int


# Git quotes a path pair as a whole, so try the unambiguous shapes first:
# both quoted, then both unquoted (a quoted path cannot contain a raw quote,
# git escapes it as \"). The loose mixed shape is a last-resort fallback.
_DIFF_GIT_QUOTED = re.compile(r'^diff --git "a/(.+?)" "b/(.+?)"$')
_DIFF_GIT_UNQUOTED = re.compile(r"^diff --git a/(.+) b/(.+)$")
_DIFF_GIT = re.compile(r'^diff --git "?a/(.+?)"? "?b/(.+?)"?$')
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: (.*))?$")
_METADATA_PREFIXES = (
    "old mode ",
    "new mode ",
    "copy from ",
    "copy to ",
    "rename old ",
    "rename new ",
    "similarity index ",
    "dissimilarity index ",
    "index ",
)


def _body_kind(line: str) -> DiffLineKind | None:
    if line.startswith(" "):
        return DiffLineKind.CONTEXT
    if line.startswith("+"):
        return DiffLineKind.ADDED
    if line.startswith("-"):
        return DiffLineKind.DELETED
    return None


def _unquote(value: str) -> str:
    """Strip a matching pair of surrounding double quotes, if present.

    Git C-escapes (`\\"`, octal like `\\303\\251`) are not decoded, so a path
    git quotes non-trivially is kept with its literal escape sequences.
    """
    if len(value) > 1 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def _split_lines(text: str) -> list[str]:
    """Split on newlines only.

    str.splitlines() would also split on \\f, \\v, \\x85 and \\u2028 inside file
    content, corrupting hunk accounting; a trailing \\r is tolerated (CRLF).
    Stripping that \\r can also drop a genuine CR at the end of a line in an
    LF diff — patch(1) makes the same tradeoff — but CRLF tolerance wins.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def _marker_path(line: str, prefix: str) -> str:
    value = line[4:]
    if "\t" in value:
        value = value.split("\t", 1)[0]
    value = _unquote(value)
    if value == "/dev/null":
        return ""
    if value.startswith(f"{prefix}/"):
        return value[len(prefix) + 1 :]
    return value


def _file_diff(
    old_path: str, new_path: str, status: FileStatus, is_binary: bool, hunks: list[DiffHunk]
) -> FileDiff:
    """Build a FileDiff, clearing the side an added or deleted file does not have."""
    if status is FileStatus.ADDED:
        old_path = ""
    elif status is FileStatus.DELETED:
        new_path = ""
    return FileDiff(old_path, new_path, status, is_binary, tuple(hunks))


def parse_diff(text: str) -> tuple[FileDiff, ...]:
    """Parse a unified git diff into per-file structures.

    Raise DiffFormatError (reason + line number, never diff content) when the
    text does not follow the unified git diff format.
    """
    if not text.strip():
        return ()

    files: list[FileDiff] = []
    hunks: list[DiffHunk] = []
    status = FileStatus.MODIFIED
    is_binary = False
    old_path = ""
    new_path = ""
    in_file = False

    hunk_open = False
    hunk_lineno = 0
    old_start = old_count = new_start = new_count = 0
    section: str | None = None
    body: list[DiffLine] = []
    old_left = new_left = 0
    old_at = new_at = 0

    for lineno, line in enumerate(_split_lines(text), start=1):
        if hunk_open:
            if line.startswith("\\"):
                continue
            kind = _body_kind(line)
            if kind is not None:
                content = line[1:]
                if kind is DiffLineKind.CONTEXT:
                    if old_left == 0 or new_left == 0:
                        raise DiffFormatError("hunk has more lines than declared", lineno)
                    body.append(DiffLine(kind, old_at, new_at, content))
                    old_left -= 1
                    new_left -= 1
                    old_at += 1
                    new_at += 1
                elif kind is DiffLineKind.ADDED:
                    if new_left == 0:
                        raise DiffFormatError("hunk has more added lines than declared", lineno)
                    body.append(DiffLine(kind, None, new_at, content))
                    new_left -= 1
                    new_at += 1
                else:
                    if old_left == 0:
                        raise DiffFormatError("hunk has more deleted lines than declared", lineno)
                    body.append(DiffLine(kind, old_at, None, content))
                    old_left -= 1
                    old_at += 1
                continue
            if old_left or new_left:
                raise DiffFormatError("hunk has fewer lines than its header declares", lineno)
            hunks.append(DiffHunk(old_start, old_count, new_start, new_count, section, tuple(body)))
            hunk_open = False

        if not line.strip():
            continue
        if line.startswith("diff --git "):
            if in_file:
                files.append(_file_diff(old_path, new_path, status, is_binary, hunks))
            match = (
                _DIFF_GIT_QUOTED.match(line)
                or _DIFF_GIT_UNQUOTED.match(line)
                or _DIFF_GIT.match(line)
            )
            if match is None:
                raise DiffFormatError("malformed 'diff --git' header", lineno)
            old_path, new_path = match.group(1), match.group(2)
            status = FileStatus.MODIFIED
            is_binary = False
            hunks = []
            in_file = True
            continue
        if not in_file:
            raise DiffFormatError("diff content outside any file section", lineno)
        if is_binary:
            continue
        if line.startswith("new file mode "):
            status = FileStatus.ADDED
        elif line.startswith("deleted file mode "):
            status = FileStatus.DELETED
        elif line.startswith("rename from "):
            status = FileStatus.RENAMED
            old_path = _unquote(line[len("rename from ") :])
        elif line.startswith("rename to "):
            status = FileStatus.RENAMED
            new_path = _unquote(line[len("rename to ") :])
        elif line.startswith("--- "):
            old_path = _marker_path(line, "a")
        elif line.startswith("+++ "):
            new_path = _marker_path(line, "b")
        elif (
            line.startswith("Binary files ")
            and line.endswith(" differ")
            or line == ("GIT binary patch")
        ):
            is_binary = True
        elif line.startswith(_METADATA_PREFIXES):
            pass
        elif line.startswith("@@"):
            match = _HUNK_HEADER.match(line)
            if match is None:
                raise DiffFormatError("malformed hunk header", lineno)
            old_start = int(match.group(1))
            old_count = 1 if match.group(2) is None else int(match.group(2))
            new_start = int(match.group(3))
            new_count = 1 if match.group(4) is None else int(match.group(4))
            if (old_start == 0 and old_count > 0) or (new_start == 0 and new_count > 0):
                raise DiffFormatError("line 0 cannot start a non-empty hunk side", lineno)
            section = match.group(5) or None
            hunk_open = True
            hunk_lineno = lineno
            body = []
            old_left, new_left = old_count, new_count
            old_at, new_at = old_start, new_start
        elif line[:1] in (" ", "+", "-"):
            raise DiffFormatError("diff body line outside a hunk", lineno)
        else:
            raise DiffFormatError("unexpected line in a file header section", lineno)

    if hunk_open:
        if old_left or new_left:
            raise DiffFormatError("hunk is truncated at the end of the diff", hunk_lineno)
        hunks.append(DiffHunk(old_start, old_count, new_start, new_count, section, tuple(body)))
    if in_file:
        files.append(_file_diff(old_path, new_path, status, is_binary, hunks))
    return tuple(files)


def _old_end(hunk: DiffHunk) -> int:
    return hunk.old_start + hunk.old_count - 1 if hunk.old_count else hunk.old_start


def _new_end(hunk: DiffHunk) -> int:
    return hunk.new_start + hunk.new_count - 1 if hunk.new_count else hunk.new_start


def _chunk_of(hunks: list[DiffHunk]) -> Chunk:
    return Chunk(
        hunks=tuple(hunks),
        old_start=hunks[0].old_start,
        old_end=_old_end(hunks[-1]),
        new_start=hunks[0].new_start,
        new_end=_new_end(hunks[-1]),
    )


def split_into_chunks(
    diff: FileDiff, *, merge_gap: int = 3, max_lines: int = 400
) -> tuple[Chunk, ...]:
    """Group a file's hunks into review chunks bounded by a gap and a line budget."""
    if diff.is_binary or not diff.hunks:
        return ()

    chunks: list[Chunk] = []
    current: list[DiffHunk] = []
    current_lines = 0
    for hunk in diff.hunks:
        if current:
            gap = hunk.old_start - _old_end(current[-1])
            if gap <= merge_gap and current_lines + gap + len(hunk.lines) <= max_lines:
                current.append(hunk)
                current_lines += gap + len(hunk.lines)
            else:
                chunks.append(_chunk_of(current))
                current = [hunk]
                current_lines = len(hunk.lines)
        else:
            current = [hunk]
            current_lines = len(hunk.lines)
    if current:
        chunks.append(_chunk_of(current))
    return tuple(chunks)
