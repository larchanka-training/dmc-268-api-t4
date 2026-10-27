"""The code around a published finding, rebuilt from the forge for the console.

Diffs are never stored (docs/PIPELINE_SPEC.md §7.4, D6), so the run screen asks for the
hunks behind each finding and, to unfold context, for ranges of the reviewed file. Both
are read live at the job's reviewed revision: the diff of `base_sha...head_sha` through
the `CompareDiffs` port, the file at `head_sha` through `RepositoryContents`. Nothing
read here is kept or logged; the results go straight back to the caller.

When the installation or a reviewed commit is gone, or the finding's line is no longer in
the diff, the service raises ContextUnavailableError and the console falls back to
`path:line` with a link to the forge.
"""

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.auth import Session
from domain.diff import DiffHunk, DiffLine, DiffLineKind, FileDiff, FileStatus, parse_diff
from domain.errors import (
    ContextUnavailableError,
    DiffFormatError,
    ForgePermissionError,
    InstallationGoneError,
)
from domain.models import DiffSide

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from domain.findings import PublishedFinding
    from domain.ports import (
        CompareDiffs,
        JobHistory,
        RepositoryContents,
        ReviewCommentStore,
        ReviewJob,
    )

DEFAULT_CONTEXT_LINES = 3
MAX_CONTEXT_LINES = 50
# A larger file still gets its hunks; only its line count falls back to the diff's.
MAX_FILE_BYTES = 1024 * 1024
# One unfold step of the console asks for a few dozen lines; this bounds a hand-made URL.
MAX_FILE_LINES = 1000


@dataclass(frozen=True, slots=True)
class FindingContext:
    path: str
    # Set for a renamed file only.
    old_path: str | None
    status: FileStatus
    # Lines in the file at the reviewed revision; bounds how far context unfolds.
    file_line_count: int
    hunks: tuple[DiffHunk, ...]


@dataclass(frozen=True, slots=True)
class FileLines:
    path: str
    # New-file number of the first line; the range is clamped to the file.
    start: int
    lines: tuple[str, ...]


class FindingContextService:
    """Reads the diff and the file behind a job's findings, on demand."""

    def __init__(
        self,
        jobs: "JobHistory",
        findings: "ReviewCommentStore",
        diffs: "CompareDiffs",
        contents: "RepositoryContents",
    ) -> None:
        self._jobs = jobs
        self._findings = findings
        self._diffs = diffs
        self._contents = contents

    async def context(
        self,
        session: Session,
        job_id: str,
        finding_id: str,
        context_lines: int = DEFAULT_CONTEXT_LINES,
    ) -> FindingContext | None:
        """The hunks that hold the finding, or None when the session cannot see the job
        or the job has no such finding.

        Raises ContextUnavailableError when the code cannot be rebuilt, and
        ForgeUnavailableError when the forge fails for a transient reason.
        """
        job = await self._visible_job(session, job_id)
        if job is None:
            return None
        finding = await self._finding(job_id, finding_id)
        if finding is None:
            return None

        file_diff = _file_of(await self._diff(job), finding.path, finding.side)
        if file_diff is None or file_diff.is_binary:
            raise ContextUnavailableError("file not in the diff")
        first = finding.line if finding.start_line is None else finding.start_line
        hunks = tuple(
            _trim_context(hunk, context_lines)
            for hunk in file_diff.hunks
            if _overlaps(hunk, finding.side, first, finding.line)
        )
        if not hunks:
            raise ContextUnavailableError("line not in the diff")

        return FindingContext(
            path=file_diff.new_path or file_diff.old_path,
            old_path=file_diff.old_path if file_diff.status is FileStatus.RENAMED else None,
            status=file_diff.status,
            file_line_count=await self._line_count(job, file_diff),
            hunks=hunks,
        )

    async def file_lines(
        self, session: Session, job_id: str, path: str, start: int, end: int
    ) -> FileLines | None:
        """Lines `start..end` (1-based, inclusive, new-file numbering) of `path` at the
        reviewed revision, clamped to the file. None when the session cannot see the job
        or no finding of the job is on `path`: the console only unfolds code it shows.

        Raises ContextUnavailableError and ForgeUnavailableError as `context` does.
        """
        job = await self._visible_job(session, job_id)
        if job is None:
            return None
        findings = await self._findings.for_job(job_id)
        if not any(finding.path == path for finding in findings):
            return None
        raw = await self._read(job, path)
        if raw is None:
            # Removed at the reviewed revision, or the commit is gone; either way there
            # is nothing at head to unfold.
            return FileLines(path=path, start=start, lines=())
        lines = _split_lines(raw)
        return FileLines(path=path, start=start, lines=tuple(lines[start - 1 : end]))

    async def _visible_job(self, session: Session, job_id: str) -> "ReviewJob | None":
        job = await self._jobs.get(job_id)
        visible = {organization.installation_id for organization in session.organizations}
        if job is None or job.payload.installation_id not in visible:
            return None
        return job

    async def _finding(self, job_id: str, finding_id: str) -> "PublishedFinding | None":
        wanted = _uuid(finding_id)
        if wanted is None:
            return None
        for finding in await self._findings.for_job(job_id):
            if _uuid(finding.id) == wanted:
                return finding
        return None

    async def _diff(self, job: "ReviewJob") -> tuple[FileDiff, ...]:
        payload = job.payload
        try:
            text = await self._diffs.compare_diff(
                payload.installation_id,
                payload.repo_full_name,
                payload.base_sha,
                payload.head_sha,
            )
        except InstallationGoneError:
            raise ContextUnavailableError("installation gone") from None
        if text is None:
            raise ContextUnavailableError("commit gone")
        try:
            return parse_diff(text)
        except DiffFormatError:
            raise ContextUnavailableError("diff unreadable") from None

    async def _read(self, job: "ReviewJob", path: str) -> bytes | None:
        payload = job.payload
        try:
            return await self._contents.read_file(
                payload.installation_id,
                payload.repo_full_name,
                path,
                payload.head_sha,
                max_bytes=MAX_FILE_BYTES,
            )
        except (InstallationGoneError, ForgePermissionError):
            raise ContextUnavailableError("installation gone") from None

    async def _line_count(self, job: "ReviewJob", file_diff: FileDiff) -> int:
        if file_diff.status is FileStatus.DELETED:
            return 0
        raw = await self._read(job, file_diff.new_path)
        if raw is None or len(raw) > MAX_FILE_BYTES:
            # Unknown: let context unfold as far as the diff itself reaches.
            return max((_new_end(hunk) for hunk in file_diff.hunks), default=0)
        return len(_split_lines(raw))


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _file_of(files: tuple[FileDiff, ...], path: str, side: DiffSide) -> FileDiff | None:
    """The file a finding is on: by its new path, or by the old one for the old side."""
    for file_diff in files:
        if path == (file_diff.old_path if side is DiffSide.OLD else file_diff.new_path):
            return file_diff
    for file_diff in files:
        if path in (file_diff.old_path, file_diff.new_path):
            return file_diff
    return None


def _overlaps(hunk: DiffHunk, side: DiffSide, first: int, last: int) -> bool:
    numbers = [_number(line, side is DiffSide.OLD) for line in hunk.lines]
    return any(number is not None and first <= number <= last for number in numbers)


def _trim_context(hunk: DiffHunk, keep: int) -> DiffHunk:
    """The hunk with at most `keep` unchanged lines before its first change and after its
    last; context between changes stays, so the hunk never hides a change."""
    lines = hunk.lines
    changed = [i for i, line in enumerate(lines) if line.kind is not DiffLineKind.CONTEXT]
    if not changed:
        return hunk
    begin = max(0, changed[0] - keep)
    kept = lines[begin : changed[-1] + keep + 1]
    if len(kept) == len(lines):
        return hunk
    old_count = sum(line.kind is not DiffLineKind.ADDED for line in kept)
    new_count = sum(line.kind is not DiffLineKind.DELETED for line in kept)
    return DiffHunk(
        old_start=_start(kept, True) if old_count else _before(hunk, begin, True),
        old_count=old_count,
        new_start=_start(kept, False) if new_count else _before(hunk, begin, False),
        new_count=new_count,
        section=hunk.section,
        lines=kept,
    )


def _number(line: DiffLine, old: bool) -> int | None:
    return line.old_lineno if old else line.new_lineno


def _start(kept: tuple[DiffLine, ...], old: bool) -> int:
    return next(n for line in kept if (n := _number(line, old)) is not None)


def _before(hunk: DiffHunk, begin: int, old: bool) -> int:
    """The start of an empty side: git puts it on the line before the change."""
    if (hunk.old_count if old else hunk.new_count) == 0:
        return hunk.old_start if old else hunk.new_start
    numbers = [n for line in hunk.lines[:begin] if (n := _number(line, old)) is not None]
    return numbers[-1] if numbers else (hunk.old_start if old else hunk.new_start) - 1


def _new_end(hunk: DiffHunk) -> int:
    return hunk.new_start + hunk.new_count - 1 if hunk.new_count else hunk.new_start


def _split_lines(raw: bytes) -> list[str]:
    """Split on newlines only, like domain.diff: str.splitlines() would also split on
    form feeds and Unicode separators inside a line."""
    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]
