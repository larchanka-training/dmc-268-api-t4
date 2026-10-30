"""Hunk selection and context trimming for a finding's code (domain.finding_context)."""

from domain.diff import DiffHunk, DiffLineKind, parse_diff
from domain.finding_context import _file_of, _overlaps, _trim_context
from domain.models import DiffSide


def hunk_of(diff: str) -> DiffHunk:
    (file_diff,) = parse_diff(diff)
    (hunk,) = file_diff.hunks
    return hunk


ADDED_ONLY = (
    "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
    "@@ -1,4 +1,6 @@\n"
    " one\n two\n+new 3\n+new 4\n three\n four\n"
)


def test_trimming_keeps_context_between_changes_and_recounts() -> None:
    hunk = hunk_of(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,9 +1,9 @@\n"
        " c1\n c2\n c3\n-o4\n+n4\n c5\n-o6\n+n6\n c7\n c8\n c9\n"
    )

    trimmed = _trim_context(hunk, 1)

    assert [line.content for line in trimmed.lines] == [
        "c3", "o4", "n4", "c5", "o6", "n6", "c7",
    ]  # fmt: skip
    assert (trimmed.old_start, trimmed.old_count) == (3, 5)
    assert (trimmed.new_start, trimmed.new_count) == (3, 5)


def test_zero_context_leaves_an_empty_side_on_the_line_before() -> None:
    trimmed = _trim_context(hunk_of(ADDED_ONLY), 0)

    assert all(line.kind is DiffLineKind.ADDED for line in trimmed.lines)
    assert (trimmed.old_start, trimmed.old_count) == (2, 0)
    assert (trimmed.new_start, trimmed.new_count) == (3, 2)


def test_a_hunk_within_the_limit_is_returned_as_is() -> None:
    hunk = hunk_of(ADDED_ONLY)

    assert _trim_context(hunk, 3) is hunk


def test_overlap_follows_the_finding_s_side_and_range() -> None:
    hunk = hunk_of(ADDED_ONLY)

    assert _overlaps(hunk, DiffSide.NEW, 3, 3)
    assert _overlaps(hunk, DiffSide.NEW, 6, 9)
    assert not _overlaps(hunk, DiffSide.NEW, 7, 9)
    assert not _overlaps(hunk, DiffSide.OLD, 5, 5)


def test_an_old_side_finding_finds_a_renamed_file_by_its_old_path() -> None:
    files = parse_diff(
        "diff --git a/old.py b/new.py\nsimilarity index 90%\n"
        "rename from old.py\nrename to new.py\n--- a/old.py\n+++ b/new.py\n"
        "@@ -1 +1 @@\n-a\n+b\n"
    )

    found = _file_of(files, "old.py", DiffSide.OLD)
    assert found is not None and found.new_path == "new.py"
    assert _file_of(files, "new.py", DiffSide.NEW) is found
    assert _file_of(files, "other.py", DiffSide.NEW) is None
