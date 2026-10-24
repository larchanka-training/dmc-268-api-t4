import pytest

from domain.diff import DiffHunk, DiffLine, DiffLineKind, FileDiff, FileStatus
from domain.diff_filter import (
    LOCKFILE_BASENAMES,
    FilterResult,
    SkippedFile,
    SkipReason,
    classify_file,
    filter_files,
)


def make_diff(
    path: str,
    *,
    status: FileStatus = FileStatus.MODIFIED,
    is_binary: bool = False,
    old_path: str | None = None,
) -> FileDiff:
    hunks = (
        ()
        if is_binary
        else (
            DiffHunk(
                old_start=1,
                old_count=1,
                new_start=1,
                new_count=1,
                section=None,
                lines=(DiffLine(DiffLineKind.CONTEXT, 1, 1, "x"),),
            ),
        )
    )
    return FileDiff(
        old_path=old_path if old_path is not None else path,
        new_path=path,
        status=status,
        is_binary=is_binary,
        hunks=hunks,
    )


def test_binary_file_is_skipped() -> None:
    assert classify_file(make_diff("logo.png", is_binary=True)) is SkipReason.BINARY


@pytest.mark.parametrize("name", sorted(LOCKFILE_BASENAMES))
def test_lockfiles_are_skipped_by_exact_basename(name: str) -> None:
    assert classify_file(make_diff(f"deps/{name}")) is SkipReason.LOCKFILE


@pytest.mark.parametrize("path", ["app.min.js", "app.min.mjs", "styles.min.css", "bundle.js.map"])
def test_minified_files_are_skipped(path: str) -> None:
    assert classify_file(make_diff(path)) is SkipReason.MINIFIED


@pytest.mark.parametrize(
    "path",
    ["vendor/lib.c", "src/node_modules/x.js", "third_party/a.py", "third-party/b.ts"],
)
def test_vendored_paths_are_skipped(path: str) -> None:
    assert classify_file(make_diff(path)) is SkipReason.VENDORED


def test_binary_takes_precedence_over_lockfile() -> None:
    diff = make_diff("package-lock.json", is_binary=True)

    assert classify_file(diff) is SkipReason.BINARY


@pytest.mark.parametrize("path", ["src/app.py", "package.json", "main.js"])
def test_reviewable_files_are_not_classified(path: str) -> None:
    assert classify_file(make_diff(path)) is None


@pytest.mark.parametrize(
    ("old_path", "reason"),
    [
        ("pnpm-lock.yaml", SkipReason.LOCKFILE),
        ("third_party/old.py", SkipReason.VENDORED),
        ("app.min.js", SkipReason.MINIFIED),
    ],
)
def test_deleted_files_are_classified_by_old_path(old_path: str, reason: SkipReason) -> None:
    diff = make_diff("", status=FileStatus.DELETED, old_path=old_path)

    result = filter_files([diff])

    assert result.kept == ()
    assert result.skipped == (SkippedFile(old_path, reason),)


def test_filter_preserves_input_order() -> None:
    app = make_diff("src/app.py")
    lock = make_diff("package-lock.json")
    logo = make_diff("logo.png", is_binary=True)
    main = make_diff("main.js")
    vendored = make_diff("vendor/lib.c")
    yarn = make_diff("yarn.lock")

    result = filter_files([app, lock, logo, main, vendored, yarn])

    assert result.kept == (app, main)
    assert result.skipped == (
        SkippedFile("package-lock.json", SkipReason.LOCKFILE),
        SkippedFile("logo.png", SkipReason.BINARY),
        SkippedFile("vendor/lib.c", SkipReason.VENDORED),
        SkippedFile("yarn.lock", SkipReason.LOCKFILE),
    )


def test_skip_counts_omit_zero_reasons_and_follow_declaration_order() -> None:
    files = [
        make_diff("logo.png", is_binary=True),
        make_diff("package-lock.json"),
        make_diff("app.min.js"),
        make_diff("yarn.lock"),
    ]

    result = filter_files(files)

    assert result.skip_counts == (
        (SkipReason.BINARY, 1),
        (SkipReason.LOCKFILE, 2),
        (SkipReason.MINIFIED, 1),
    )


def test_skip_counts_cover_every_reason_when_all_present() -> None:
    files = [
        make_diff("app.min.js"),
        make_diff("vendor/lib.c"),
        make_diff("logo.png", is_binary=True),
        make_diff("go.sum"),
    ]

    result = filter_files(files)

    assert result.skip_counts == (
        (SkipReason.BINARY, 1),
        (SkipReason.LOCKFILE, 1),
        (SkipReason.MINIFIED, 1),
        (SkipReason.VENDORED, 1),
    )


def test_empty_input_yields_empty_result() -> None:
    result = filter_files([])

    assert result == FilterResult((), ())
    assert result.skip_counts == ()
