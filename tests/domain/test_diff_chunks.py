from domain.diff import (
    Chunk,
    DiffHunk,
    DiffLine,
    DiffLineKind,
    FileDiff,
    FileStatus,
    split_into_chunks,
)


def context_hunk(old_start: int, new_start: int, count: int) -> DiffHunk:
    lines = tuple(
        DiffLine(
            kind=DiffLineKind.CONTEXT,
            old_lineno=old_start + offset,
            new_lineno=new_start + offset,
            content=f"line {old_start + offset}",
        )
        for offset in range(count)
    )
    return DiffHunk(
        old_start=old_start,
        old_count=count,
        new_start=new_start,
        new_count=count,
        section=None,
        lines=lines,
    )


def modified_file(*hunks: DiffHunk) -> FileDiff:
    return FileDiff(
        old_path="src/x.py",
        new_path="src/x.py",
        status=FileStatus.MODIFIED,
        is_binary=False,
        hunks=hunks,
    )


def test_hunks_within_the_merge_gap_are_merged_into_one_chunk() -> None:
    first = context_hunk(1, 1, 3)
    second = context_hunk(6, 6, 2)

    chunks = split_into_chunks(modified_file(first, second))

    assert chunks == (
        Chunk(
            hunks=(first, second),
            old_start=1,
            old_end=7,
            new_start=1,
            new_end=7,
        ),
    )


def test_hunks_beyond_the_merge_gap_are_split() -> None:
    first = context_hunk(1, 1, 3)
    second = context_hunk(8, 8, 2)

    chunks = split_into_chunks(modified_file(first, second))

    assert chunks == (
        Chunk(hunks=(first,), old_start=1, old_end=3, new_start=1, new_end=3),
        Chunk(hunks=(second,), old_start=8, old_end=9, new_start=8, new_end=9),
    )


def test_chunks_split_at_max_lines() -> None:
    first = context_hunk(1, 1, 4)
    second = context_hunk(7, 7, 3)
    third = context_hunk(12, 12, 2)

    chunks = split_into_chunks(modified_file(first, second, third), max_lines=10)

    assert chunks == (
        Chunk(
            hunks=(first, second),
            old_start=1,
            old_end=9,
            new_start=1,
            new_end=9,
        ),
        Chunk(hunks=(third,), old_start=12, old_end=13, new_start=12, new_end=13),
    )


def test_an_oversized_hunk_stays_alone() -> None:
    oversized = context_hunk(1, 1, 15)
    following = context_hunk(17, 17, 2)

    chunks = split_into_chunks(modified_file(oversized, following), max_lines=10)

    assert chunks == (
        Chunk(hunks=(oversized,), old_start=1, old_end=15, new_start=1, new_end=15),
        Chunk(hunks=(following,), old_start=17, old_end=18, new_start=17, new_end=18),
    )


def test_a_binary_file_yields_no_chunks() -> None:
    file = FileDiff(
        old_path="logo.png",
        new_path="logo.png",
        status=FileStatus.MODIFIED,
        is_binary=True,
        hunks=(context_hunk(1, 1, 1),),
    )

    assert split_into_chunks(file) == ()


def test_a_file_without_hunks_yields_no_chunks() -> None:
    file = FileDiff(
        old_path="src/old.py",
        new_path="src/new.py",
        status=FileStatus.RENAMED,
        is_binary=False,
        hunks=(),
    )

    assert split_into_chunks(file) == ()
