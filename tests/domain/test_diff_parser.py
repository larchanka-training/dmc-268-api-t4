import pytest

from domain.diff import (
    DiffHunk,
    DiffLine,
    DiffLineKind,
    FileDiff,
    FileStatus,
    parse_diff,
)
from domain.errors import DiffFormatError

SAMPLE_DIFF = """
diff --git a/src/new_file.py b/src/new_file.py
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/src/new_file.py
@@ -0,0 +1,3 @@
+def hello():
+    return "hello"
+# end
diff --git a/src/mod.py b/src/mod.py
index 2222222..3333333 100644
--- a/src/mod.py
+++ b/src/mod.py
@@ -1,4 +1,4 @@
 context one
 context two
 context three
-import old
+import new
@@ -8,2 +9,3 @@ def main():
 context eight
+added nine
 context nine
\\ No newline at end of file
diff --git a/src/tail.py b/src/tail.py
index 9999999..aaaaaaa 100644
--- a/src/tail.py
+++ b/src/tail.py
@@ -3 +3 @@
 last line
\\ No newline at end of file
diff --git a/src/gone.py b/src/gone.py
deleted file mode 100644
index 4444444..0000000
--- a/src/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-line one
-line two
diff --git a/src/old_name.py b/src/renamed.py
similarity index 95%
rename from src/old_name.py
rename to src/renamed.py
index 5555555..6666666 100644
diff --git a/logo.png b/logo.png
index 7777777..8888888 100644
Binary files a/logo.png and b/logo.png differ
"""


def test_parses_paths_statuses_and_binary_flags() -> None:
    files = parse_diff(SAMPLE_DIFF)

    assert [file.old_path for file in files] == [
        "",
        "src/mod.py",
        "src/tail.py",
        "src/gone.py",
        "src/old_name.py",
        "logo.png",
    ]
    assert [file.new_path for file in files] == [
        "src/new_file.py",
        "src/mod.py",
        "src/tail.py",
        "",
        "src/renamed.py",
        "logo.png",
    ]
    assert [file.status for file in files] == [
        FileStatus.ADDED,
        FileStatus.MODIFIED,
        FileStatus.MODIFIED,
        FileStatus.DELETED,
        FileStatus.RENAMED,
        FileStatus.MODIFIED,
    ]
    assert [file.is_binary for file in files] == [False, False, False, False, False, True]
    assert [len(file.hunks) for file in files] == [1, 2, 1, 1, 0, 0]


def test_added_file_hunk_is_numbered_from_one() -> None:
    added = parse_diff(SAMPLE_DIFF)[0]

    assert added.hunks == (
        DiffHunk(
            old_start=0,
            old_count=0,
            new_start=1,
            new_count=3,
            section=None,
            lines=(
                DiffLine(DiffLineKind.ADDED, None, 1, "def hello():"),
                DiffLine(DiffLineKind.ADDED, None, 2, '    return "hello"'),
                DiffLine(DiffLineKind.ADDED, None, 3, "# end"),
            ),
        ),
    )


def test_modified_file_keeps_two_hunks_with_section_and_numbering() -> None:
    modified = parse_diff(SAMPLE_DIFF)[1]
    first, second = modified.hunks

    assert first == DiffHunk(
        old_start=1,
        old_count=4,
        new_start=1,
        new_count=4,
        section=None,
        lines=(
            DiffLine(DiffLineKind.CONTEXT, 1, 1, "context one"),
            DiffLine(DiffLineKind.CONTEXT, 2, 2, "context two"),
            DiffLine(DiffLineKind.CONTEXT, 3, 3, "context three"),
            DiffLine(DiffLineKind.DELETED, 4, None, "import old"),
            DiffLine(DiffLineKind.ADDED, None, 4, "import new"),
        ),
    )
    assert second == DiffHunk(
        old_start=8,
        old_count=2,
        new_start=9,
        new_count=3,
        section="def main():",
        lines=(
            DiffLine(DiffLineKind.CONTEXT, 8, 9, "context eight"),
            DiffLine(DiffLineKind.ADDED, None, 10, "added nine"),
            DiffLine(DiffLineKind.CONTEXT, 9, 11, "context nine"),
        ),
    )


def test_omitted_hunk_count_means_one_and_no_newline_marker_is_skipped() -> None:
    tail = parse_diff(SAMPLE_DIFF)[2]

    assert tail.hunks == (
        DiffHunk(
            old_start=3,
            old_count=1,
            new_start=3,
            new_count=1,
            section=None,
            lines=(DiffLine(DiffLineKind.CONTEXT, 3, 3, "last line"),),
        ),
    )


def test_deleted_file_lines_carry_only_old_line_numbers() -> None:
    gone = parse_diff(SAMPLE_DIFF)[3]

    assert gone.hunks == (
        DiffHunk(
            old_start=1,
            old_count=2,
            new_start=0,
            new_count=0,
            section=None,
            lines=(
                DiffLine(DiffLineKind.DELETED, 1, None, "line one"),
                DiffLine(DiffLineKind.DELETED, 2, None, "line two"),
            ),
        ),
    )


def test_renamed_file_has_no_hunks_and_is_not_binary() -> None:
    renamed = parse_diff(SAMPLE_DIFF)[4]

    assert renamed.old_path == "src/old_name.py"
    assert renamed.new_path == "src/renamed.py"
    assert renamed.status is FileStatus.RENAMED
    assert renamed.is_binary is False
    assert renamed.hunks == ()


def test_added_file_without_markers_has_an_empty_old_path() -> None:
    text = "diff --git a/empty.txt b/empty.txt\nnew file mode 100644\nindex 0000000..e69de29\n"

    (file,) = parse_diff(text)

    assert file.status is FileStatus.ADDED
    assert file.old_path == ""
    assert file.new_path == "empty.txt"
    assert file.hunks == ()


def test_mode_only_change_yields_a_modified_file_without_hunks() -> None:
    text = "diff --git a/script.sh b/script.sh\nold mode 100644\nnew mode 100755\n"

    (file,) = parse_diff(text)

    assert file.old_path == "script.sh"
    assert file.new_path == "script.sh"
    assert file.status is FileStatus.MODIFIED
    assert file.is_binary is False
    assert file.hunks == ()


def test_quoted_paths_with_spaces_are_parsed() -> None:
    text = (
        'diff --git "a/src/new file.py" "b/src/new file.py"\n'
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        '+++ "b/src/new file.py"\n'
        "@@ -0,0 +1,1 @@\n"
        "+hello\n"
    )

    (file,) = parse_diff(text)

    assert file.status is FileStatus.ADDED
    assert file.old_path == ""
    assert file.new_path == "src/new file.py"
    assert file.hunks[0].lines == (DiffLine(DiffLineKind.ADDED, None, 1, "hello"),)


def test_quoted_path_containing_space_b_slash_is_split_correctly() -> None:
    text = 'diff --git "a/src b/x.py" "b/src b/x.py"\n'

    (file,) = parse_diff(text)

    assert file.old_path == "src b/x.py"
    assert file.new_path == "src b/x.py"


def test_quoted_rename_paths_are_parsed() -> None:
    text = (
        'diff --git "a/old name.py" "b/new name.py"\n'
        "similarity index 90%\n"
        'rename from "old name.py"\n'
        'rename to "new name.py"\n'
        "index 5555555..6666666 100644\n"
    )

    (file,) = parse_diff(text)

    assert file.status is FileStatus.RENAMED
    assert file.old_path == "old name.py"
    assert file.new_path == "new name.py"
    assert file.hunks == ()


def test_form_feed_and_vertical_tab_in_content_do_not_split_hunk_lines() -> None:
    content = "before\x0cbetween\x0bafter"
    text = f"diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,2 @@\n base\n+{content}\n"

    (file,) = parse_diff(text)

    assert file.hunks[0].lines == (
        DiffLine(DiffLineKind.CONTEXT, 1, 1, "base"),
        DiffLine(DiffLineKind.ADDED, None, 2, content),
    )


def test_crlf_line_endings_are_tolerated() -> None:
    text = (
        "diff --git a/x.py b/x.py\r\n"
        "--- a/x.py\r\n"
        "+++ b/x.py\r\n"
        "@@ -1,1 +1,2 @@\r\n"
        " base\r\n"
        "+new\r\n"
    )

    (file,) = parse_diff(text)

    assert file.hunks[0].lines == (
        DiffLine(DiffLineKind.CONTEXT, 1, 1, "base"),
        DiffLine(DiffLineKind.ADDED, None, 2, "new"),
    )


def test_diff_line_repr_hides_content() -> None:
    line = DiffLine(DiffLineKind.CONTEXT, 1, 1, "SECRET SOURCE LINE")

    assert "SECRET SOURCE LINE" not in repr(line)


def test_binary_file_is_flagged_without_hunks() -> None:
    binary = parse_diff(SAMPLE_DIFF)[5]

    assert binary.is_binary is True
    assert binary.hunks == ()


def test_git_binary_patch_is_flagged_and_payload_skipped() -> None:
    text = (
        "diff --git a/img.png b/img.png\n"
        "index 1234567..89abcde 100644\n"
        "GIT binary patch\n"
        "literal 42\n"
        "zcmex_the_payload"
    )

    (file,) = parse_diff(text)

    assert file.is_binary is True
    assert file.hunks == ()


@pytest.mark.parametrize("text", ["", "\n", "   \n\t\n"])
def test_empty_or_whitespace_input_yields_no_files(text: str) -> None:
    assert parse_diff(text) == ()


def test_body_line_before_any_hunk_is_rejected() -> None:
    text = "diff --git a/x.py b/x.py\nindex 0000000..1111111\n--- a/x.py\n+++ b/x.py\n+premature\n"

    with pytest.raises(DiffFormatError) as excinfo:
        parse_diff(text)

    assert excinfo.value.line_number == 5
    assert "outside a hunk" in str(excinfo.value)
    assert "premature" not in str(excinfo.value)


def test_hunk_header_with_garbage_numbers_is_rejected() -> None:
    text = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -abc +1,2 @@\n+x\n"

    with pytest.raises(DiffFormatError) as excinfo:
        parse_diff(text)

    assert excinfo.value.line_number == 4
    assert "hunk header" in str(excinfo.value)


def test_truncated_hunk_at_end_of_input_is_rejected() -> None:
    text = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n ctx\n+new\n"

    with pytest.raises(DiffFormatError) as excinfo:
        parse_diff(text)

    assert excinfo.value.line_number == 4


def test_hunk_interrupted_by_next_file_is_rejected() -> None:
    text = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,3 +1,3 @@\n"
        " ctx\n"
        "diff --git a/y.py b/y.py\n"
    )

    with pytest.raises(DiffFormatError) as excinfo:
        parse_diff(text)

    assert excinfo.value.line_number == 6


def test_hunk_with_more_lines_than_declared_is_rejected() -> None:
    text = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n ctx\n+extra\n"

    with pytest.raises(DiffFormatError) as excinfo:
        parse_diff(text)

    assert excinfo.value.line_number == 6


def test_hunk_header_without_preceding_file_is_rejected() -> None:
    with pytest.raises(DiffFormatError) as excinfo:
        parse_diff("@@ -1,2 +1,2 @@\n ctx\n")

    assert excinfo.value.line_number == 1


@pytest.mark.parametrize(
    ("kind", "old_lineno", "new_lineno"),
    [
        (DiffLineKind.ADDED, 1, 1),
        (DiffLineKind.ADDED, None, None),
        (DiffLineKind.DELETED, None, 1),
        (DiffLineKind.DELETED, None, None),
        (DiffLineKind.CONTEXT, None, 1),
        (DiffLineKind.CONTEXT, 1, None),
        (DiffLineKind.CONTEXT, 0, 1),
        (DiffLineKind.ADDED, None, 0),
    ],
)
def test_invalid_diff_line_combinations_are_refused(
    kind: DiffLineKind, old_lineno: int | None, new_lineno: int | None
) -> None:
    with pytest.raises(ValueError):
        DiffLine(kind, old_lineno, new_lineno, "x")


def test_diff_line_refuses_embedded_newline() -> None:
    with pytest.raises(ValueError):
        DiffLine(DiffLineKind.CONTEXT, 1, 1, "a\nb")


def _line(kind: DiffLineKind, old: int | None, new: int | None) -> DiffLine:
    return DiffLine(kind, old, new, "x")


def test_hunk_refuses_line_counts_that_do_not_match_its_lines() -> None:
    with pytest.raises(ValueError, match="old_count"):
        DiffHunk(1, 2, 1, 1, None, (_line(DiffLineKind.CONTEXT, 1, 1),))
    with pytest.raises(ValueError, match="new_count"):
        DiffHunk(1, 1, 1, 2, None, (_line(DiffLineKind.CONTEXT, 1, 1),))


@pytest.mark.parametrize(("old_start", "old_count"), [(0, 1), (-1, 0), (1, -1)])
def test_hunk_refuses_bad_starts_and_counts(old_start: int, old_count: int) -> None:
    with pytest.raises(ValueError):
        DiffHunk(old_start, old_count, 1, 0, None, ())


def test_hunk_accepts_start_zero_only_with_count_zero() -> None:
    DiffHunk(0, 0, 1, 0, None, ())
    DiffHunk(1, 0, 1, 0, None, ())


def test_file_diff_refuses_empty_paths() -> None:
    with pytest.raises(ValueError):
        FileDiff("", "x.py", FileStatus.MODIFIED, False, ())


def test_file_diff_refuses_status_path_inconsistency() -> None:
    FileDiff("", "x.py", FileStatus.ADDED, False, ())
    FileDiff("x.py", "", FileStatus.DELETED, False, ())
    FileDiff("x.py", "x.py", FileStatus.MODIFIED, False, ())
    FileDiff("old.py", "new.py", FileStatus.RENAMED, False, ())
    with pytest.raises(ValueError, match="added.*old_path"):
        FileDiff("x.py", "x.py", FileStatus.ADDED, False, ())
    with pytest.raises(ValueError, match="added.*new_path"):
        FileDiff("", "", FileStatus.ADDED, False, ())
    with pytest.raises(ValueError, match="deleted.*new_path"):
        FileDiff("x.py", "x.py", FileStatus.DELETED, False, ())
    with pytest.raises(ValueError, match="deleted.*old_path"):
        FileDiff("", "", FileStatus.DELETED, False, ())
    with pytest.raises(ValueError, match="modified"):
        FileDiff("", "x.py", FileStatus.MODIFIED, False, ())
    with pytest.raises(ValueError, match="renamed"):
        FileDiff("old.py", "", FileStatus.RENAMED, False, ())
