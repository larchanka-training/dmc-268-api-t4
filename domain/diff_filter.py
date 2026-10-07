"""Decide which changed files are worth reviewing.

Pure classification over FileDiff structures: stdlib only, no I/O. Skipped
files carry only a path and a reason, never diff content.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from domain.diff import FileDiff


class SkipReason(StrEnum):
    BINARY = "binary"
    LOCKFILE = "lockfile"
    MINIFIED = "minified"
    VENDORED = "vendored"


@dataclass(frozen=True, slots=True)
class SkippedFile:
    path: str
    reason: SkipReason


@dataclass(frozen=True, slots=True)
class FilterResult:
    kept: tuple[FileDiff, ...]
    skipped: tuple[SkippedFile, ...]

    @property
    def skip_counts(self) -> tuple[tuple[SkipReason, int], ...]:
        counts = Counter(skipped.reason for skipped in self.skipped)
        return tuple((reason, counts[reason]) for reason in SkipReason if counts[reason])


LOCKFILE_BASENAMES: frozenset[str] = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "Cargo.lock",
        "go.sum",
        "composer.lock",
        "Gemfile.lock",
        "flake.lock",
        "Podfile.lock",
        "deno.lock",
        "packages.lock.json",
        "paket.lock",
    }
)
MINIFIED_SUFFIXES: tuple[str, ...] = (".min.js", ".min.mjs", ".min.css", ".map")
VENDORED_SEGMENTS: frozenset[str] = frozenset(
    {"vendor", "node_modules", "third_party", "third-party"}
)


def _classified_path(diff: FileDiff) -> str:
    """The path a file is known by: its new side, or the old one for deletions.

    new_path is empty exactly for deleted files (FileDiff enforces it), so the
    old path is the fallback there.
    """
    return diff.new_path if diff.new_path else diff.old_path


def classify_file(diff: FileDiff) -> SkipReason | None:
    """Return the first reason to skip this file, or None if it is reviewable.

    Checked in order: binary, lockfile (exact basename), minified (basename
    suffix), vendored (any path segment).
    """
    path = PurePosixPath(_classified_path(diff))
    if diff.is_binary:
        return SkipReason.BINARY
    if path.name in LOCKFILE_BASENAMES:
        return SkipReason.LOCKFILE
    if path.name.endswith(MINIFIED_SUFFIXES):
        return SkipReason.MINIFIED
    if VENDORED_SEGMENTS.intersection(path.parts):
        return SkipReason.VENDORED
    return None


def filter_files(files: Sequence[FileDiff]) -> FilterResult:
    """Split parsed files into kept and skipped, preserving input order.

    Classification uses new_path, falling back to old_path when there is no
    new side: deleted files (whose new side is empty) are classified by
    the path they were known by.
    """
    kept: list[FileDiff] = []
    skipped: list[SkippedFile] = []
    for diff in files:
        reason = classify_file(diff)
        if reason is None:
            kept.append(diff)
        else:
            skipped.append(SkippedFile(_classified_path(diff), reason))
    return FilterResult(tuple(kept), tuple(skipped))
