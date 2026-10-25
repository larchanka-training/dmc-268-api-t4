"""Splitting a raw diff into one text per file, for the per-file review scope."""

from domain.diff import parse_diff, split_by_file

FILE_A = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
FILE_B = "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-b = 1\n+b = 2\n"


def test_each_file_comes_back_as_its_own_diff() -> None:
    parts = split_by_file(FILE_A + FILE_B)

    assert parts == (FILE_A, FILE_B)
    assert [parse_diff(part)[0].new_path for part in parts] == ["a.py", "b.py"]


def test_the_pieces_join_back_into_the_original_text() -> None:
    text = "From a commit message\n" + FILE_A + FILE_B

    parts = split_by_file(text)

    assert "".join(parts) == text
    assert parts[0].startswith("From a commit message\ndiff --git a/a.py")


def test_a_hunk_line_mentioning_a_header_does_not_split_the_file() -> None:
    tricky = FILE_A.replace("+a = 2\n", "+a = 2\n+diff --git a/x b/x\n").replace(
        "@@ -1 +1 @@", "@@ -1 +1,2 @@"
    )

    assert split_by_file(tricky + FILE_B) == (tricky, FILE_B)


def test_text_without_a_file_header_comes_back_whole() -> None:
    assert split_by_file("@@ -1 +1 @@\n-a\n+b\n") == ("@@ -1 +1 @@\n-a\n+b\n",)
    assert split_by_file("") == ("",)
