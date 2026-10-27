"""The enriched review context: selection, tiers, fallback, budget (review-context spec).

Pure domain tests: the AST arrives through a fake `SymbolIndex`, so nothing here
touches tree-sitter or the network.
"""

import pytest

from domain.context import (
    MAX_CONTEXT_FILES,
    ContextCut,
    build_context,
    select_context_files,
)
from domain.diff import (
    DiffHunk,
    DiffLine,
    DiffLineKind,
    FileDiff,
    FileStatus,
)
from domain.errors import SymbolParseError
from domain.ports import ContextBlock, ContextBlockKind, FileContext, SymbolSpan


def make_file(path: str, *, hunks: int = 1, status: FileStatus = FileStatus.MODIFIED) -> FileDiff:
    """A file whose hunk adds one line at new line 2 of the head revision."""
    file_hunks = tuple(
        DiffHunk(
            old_start=1 + index,
            old_count=1,
            new_start=1 + index,
            new_count=2,
            section=None,
            lines=(
                DiffLine(DiffLineKind.CONTEXT, 1 + index, 1 + index, "keep"),
                DiffLine(DiffLineKind.ADDED, None, 2 + index, "change"),
            ),
        )
        for index in range(hunks)
    )
    return FileDiff(path, path, status, False, file_hunks)


class FakeIndex:
    """Answers scripted per path; records the lines it was asked about."""

    def __init__(
        self,
        *,
        spans: dict[str, tuple[SymbolSpan, ...]] | None = None,
        called: dict[str, tuple[SymbolSpan, ...]] | None = None,
        unsupported: frozenset[str] = frozenset(),
        broken: frozenset[str] = frozenset(),
    ) -> None:
        self.spans = spans or {}
        self.called = called or {}
        self.unsupported = unsupported
        self.broken = broken
        self.asked: dict[str, tuple[int, ...]] = {}

    def supports(self, path: str) -> bool:
        return path not in self.unsupported

    def enclosing(self, path: str, source: str, lines) -> tuple[SymbolSpan, ...]:
        self.asked[path] = tuple(lines)
        if path in self.broken:
            raise SymbolParseError("errors at the hunk")
        return self.spans.get(path, ())

    def called_from(self, path: str, source: str, lines) -> tuple[SymbolSpan, ...]:
        if path in self.broken:
            raise SymbolParseError("errors at the hunk")
        return self.called.get(path, ())


PY_SOURCE = "\n".join(
    [
        "import os",  # 1
        "",  # 2
        "def helper(x):",  # 3
        "    '''Docs.'''",  # 4
        "    return x + 1",  # 5
        "",  # 6
        "def run(a, b):",  # 7
        "    '''Runs.'''",  # 8
        "    return helper(a) + b",  # 9
        "",  # 10
    ]
)
HELPER = SymbolSpan("helper", "function", 3, 5, 4)
RUN = SymbolSpan("run", "function", 7, 9, 8)


def estimate(text: str) -> int:
    return max(1, len(text) // 3)


def blocks_of(
    files,
    sources,
    index,
    *,
    token_budget: int = 10**9,
) -> tuple[tuple[FileContext, ...], int]:
    """Build with a huge budget and return the contexts plus their token cost."""
    contexts, _ = build_context(files, sources, index, token_budget=token_budget, estimate=estimate)
    cost = sum(estimate(block.text) for context in contexts for block in context.blocks)
    return contexts, cost


# --- selection (§1.3 caps) --------------------------------------------------


def test_selection_drops_binary_deleted_and_caps_the_count() -> None:
    files = tuple(make_file(f"f{i}.py") for i in range(MAX_CONTEXT_FILES + 10))
    selected = select_context_files(files)
    assert len(selected) == MAX_CONTEXT_FILES
    assert selected[0].new_path == "f0.py"
    assert selected[-1].new_path == f"f{MAX_CONTEXT_FILES - 1}.py"

    deleted = FileDiff("gone.py", "", FileStatus.DELETED, False, ())
    binary = FileDiff("b.bin", "b.bin", FileStatus.MODIFIED, True, ())
    assert select_context_files((deleted, binary)) == ()


# --- ports models (2.1) ------------------------------------------------------


def test_context_block_and_file_context_models() -> None:
    block = ContextBlock(
        kind=ContextBlockKind.ENCLOSING,
        name="run",
        start_line=7,
        end_line=9,
        text="def run(a, b):",
    )
    context = FileContext(path="app/x.py", blocks=(block,))
    assert context.path == "app/x.py"
    assert "def run" not in repr(block)
    assert "def run" not in repr(context)


def test_symbol_span_validates_kind_and_lines() -> None:
    with pytest.raises(ValueError):
        SymbolSpan("run", "module", 1, 2, 2)
    with pytest.raises(ValueError):
        SymbolSpan("run", "function", 5, 4, 3)
    assert RUN.signature_end_line == 8


# --- assembly (2.2) ----------------------------------------------------------


def test_tier1_and_tier2_blocks_come_from_the_source() -> None:
    index = FakeIndex(spans={"app/x.py": (RUN,)}, called={"app/x.py": (HELPER,)})
    file = make_file("app/x.py")
    contexts, metrics = build_context(
        (file,), {"app/x.py": PY_SOURCE}, index, token_budget=10**9, estimate=estimate
    )
    assert len(contexts) == 1
    kinds = [
        (block.kind, block.name, block.start_line, block.end_line) for block in contexts[0].blocks
    ]
    # Tier 1: the whole symbol; tier 2: header + docstring only.
    assert kinds == [
        (ContextBlockKind.ENCLOSING, "run", 7, 9),
        (ContextBlockKind.CALLED, "helper", 3, 4),
    ]
    assert contexts[0].blocks[0].text == "def run(a, b):\n    '''Runs.'''\n    return helper(a) + b"
    assert contexts[0].blocks[1].text == "def helper(x):\n    '''Docs.'''"
    assert metrics.tier1_symbols == 1
    assert metrics.tier2_symbols == 1
    assert metrics.context_cut is ContextCut.NONE
    assert metrics.context_files == 1
    # The enclosing lookup is anchored at the added line, not the context line.
    assert index.asked["app/x.py"] == (2,)


def test_unsupported_language_gets_fallback_window() -> None:
    index = FakeIndex(unsupported=frozenset({"app/x.rb"}))
    file = make_file("app/x.rb")
    source = "\n".join(f"line{i}" for i in range(1, 101))
    contexts, metrics = build_context(
        (file,), {"app/x.rb": source}, index, token_budget=10**9, estimate=estimate
    )
    assert len(contexts) == 1
    (block,) = contexts[0].blocks
    assert block.kind is ContextBlockKind.WINDOW
    # The hunk adds line 2; the window is 2 +/- 20 lines, clamped at 1.
    assert (block.start_line, block.end_line) == (1, 22)
    assert metrics.fallback_files == 1
    assert metrics.parse_errors == 0


def test_parse_error_falls_back_and_counts() -> None:
    index = FakeIndex(broken=frozenset({"app/x.py"}))
    file = make_file("app/x.py")
    contexts, metrics = build_context(
        (file,), {"app/x.py": PY_SOURCE}, index, token_budget=10**9, estimate=estimate
    )
    assert contexts[0].blocks[0].kind is ContextBlockKind.WINDOW
    assert metrics.parse_errors == 1
    assert metrics.fallback_files == 1


def test_unfetched_file_gets_no_context_block() -> None:
    index = FakeIndex(spans={"app/x.py": (RUN,)})
    file = make_file("app/x.py")
    contexts, metrics = build_context((file,), {}, index, token_budget=10**9, estimate=estimate)
    assert contexts == ()
    assert metrics.context_files == 0


def test_budget_cuts_tier2_first() -> None:
    index = FakeIndex(spans={"app/x.py": (RUN,)}, called={"app/x.py": (HELPER,)})
    file = make_file("app/x.py")
    full, cost = blocks_of((file,), {"app/x.py": PY_SOURCE}, index)
    contexts, metrics = build_context(
        (file,), {"app/x.py": PY_SOURCE}, index, token_budget=cost - 1, estimate=estimate
    )
    assert metrics.context_cut is ContextCut.TIER2
    assert [block.kind for block in contexts[0].blocks] == [ContextBlockKind.ENCLOSING]
    assert metrics.tier2_symbols == 0
    assert len(full[0].blocks) == 2


def test_budget_cuts_tier1_from_the_end_of_diff_order() -> None:
    spans = {f"f{i}.py": (RUN,) for i in range(3)}
    index = FakeIndex(spans=spans)
    files = tuple(make_file(f"f{i}.py") for i in range(3))
    sources = {f"f{i}.py": PY_SOURCE for i in range(3)}
    _, one_file_cost = blocks_of(files[:1], {"f0.py": PY_SOURCE}, index)
    contexts, metrics = build_context(
        files, sources, index, token_budget=one_file_cost, estimate=estimate
    )
    assert metrics.context_cut is ContextCut.TIER1
    assert [context.path for context in contexts] == ["f0.py"]
    assert metrics.tier1_symbols == 1


def test_budget_cuts_fallback_windows_last() -> None:
    index = FakeIndex(unsupported=frozenset({"f0.rb", "f1.rb"}))
    files = (make_file("f0.rb"), make_file("f1.rb"))
    sources = {
        "f0.rb": "\n".join(f"l{i}" for i in range(1, 200)),
        "f1.rb": "\n".join(f"l{i}" for i in range(1, 200)),
    }
    _, one_window_cost = blocks_of(files[:1], {"f0.rb": sources["f0.rb"]}, index)
    contexts, metrics = build_context(
        files, sources, index, token_budget=one_window_cost, estimate=estimate
    )
    assert metrics.context_cut is ContextCut.FALLBACK
    assert len(contexts) == 1
    assert contexts[0].blocks[0].kind is ContextBlockKind.WINDOW


def test_huge_budget_keeps_everything() -> None:
    index = FakeIndex(spans={"app/x.py": (RUN,)}, called={"app/x.py": (HELPER,)})
    file = make_file("app/x.py")
    contexts, metrics = build_context(
        (file,), {"app/x.py": PY_SOURCE}, index, token_budget=10**9, estimate=estimate
    )
    assert metrics.context_cut is ContextCut.NONE
    assert len(contexts[0].blocks) == 2
