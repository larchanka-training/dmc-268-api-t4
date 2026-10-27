"""Assemble the enriched review context around a diff's changed lines.

Pure domain code (AGENTS.md rule 1): no adapters imports, no I/O. The syntax
tree arrives through the `SymbolIndex` port and the token estimator is injected,
so the same assembly runs in tests against fakes. Everything here follows
docs/CONTEXT_AND_VERIFICATION_SPEC.md §1 ([C1]-[C6]) and the review-context
delta spec: tiers and cut order, fetch caps, the fallback window, and metrics
as counters only — context text is untrusted data, never stored or logged.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from domain.diff import DiffLineKind, FileDiff, FileStatus
from domain.errors import SymbolParseError
from domain.ports import ContextBlock, ContextBlockKind, FileContext, SymbolIndex, SymbolSpan

#: docs/CONTEXT_AND_VERIFICATION_SPEC.md §1.3 [C3]: at most this many source
#: files are fetched per job; the rest keep only their hunk lines.
MAX_CONTEXT_FILES = 30

#: §1.3 [C3]: a larger file is not fetched (its hunks stay its only context).
MAX_SOURCE_BYTES = 512 * 1024

#: §1.2 [C2]: the fallback window around a hunk when the AST is unavailable.
FALLBACK_PAD_LINES = 20


class ContextCut(StrEnum):
    """How deeply the context had to be cut to fit the budget (§1.4 [C4])."""

    NONE = "none"
    TIER2 = "tier2"
    TIER1 = "tier1"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class ContextMetrics:
    """§1.6 [C6]: what the context pass looked like, counts only.

    The counts describe the context that reached the prompt. `context_fetches`
    is filled by the caller (the worker), which is the side doing the I/O.
    """

    context_files: int = 0
    tier1_symbols: int = 0
    tier2_symbols: int = 0
    fallback_files: int = 0
    parse_errors: int = 0
    context_tokens: int = 0
    context_cut: ContextCut = ContextCut.NONE


def select_context_files(
    files: Sequence[FileDiff], *, max_files: int = MAX_CONTEXT_FILES
) -> tuple[FileDiff, ...]:
    """The files worth fetching source for, in diff order, capped ([C3]).

    Binary files, deleted files (the diff already holds the whole old file) and
    files without hunks are never fetched. Files past `max_files` are simply
    not selected: their hunk lines are their context.
    """
    eligible = [
        diff
        for diff in files
        if not diff.is_binary
        and diff.hunks
        and diff.status is not FileStatus.DELETED
        and diff.new_path
    ]
    return tuple(eligible[:max_files])


def _anchor_lines(diff: FileDiff) -> tuple[int, ...]:
    """The head-revision lines a change is anchored to.

    Added lines carry their new line number. A hunk of pure deletions has no
    new lines, so it anchors to `new_start`: the position the deletions
    collapsed to, which the enclosing symbol is read against.
    """
    anchors: list[int] = []
    for hunk in diff.hunks:
        added = [
            line.new_lineno
            for line in hunk.lines
            if line.kind is DiffLineKind.ADDED and line.new_lineno is not None
        ]
        if added:
            anchors.extend(added)
        elif hunk.new_start >= 1:
            anchors.append(hunk.new_start)
    return tuple(dict.fromkeys(anchors))


def _slice(source_lines: Sequence[str], start: int, end: int) -> str:
    """The 1-based inclusive line range, clamped to the file."""
    start = max(1, start)
    end = min(len(source_lines), end)
    return "\n".join(source_lines[start - 1 : end])


def _fallback_windows(
    diff: FileDiff, source_lines: Sequence[str], *, pad: int
) -> tuple[ContextBlock, ...]:
    """A window around each hunk, merges of overlapping windows first ([C2])."""
    spans: list[tuple[int, int]] = []
    for hunk in diff.hunks:
        new_end = hunk.new_start + hunk.new_count - 1 if hunk.new_count else hunk.new_start
        start = max(1, hunk.new_start - pad)
        end = min(len(source_lines), new_end + pad)
        if start > len(source_lines) or end < 1 or start > end:
            continue
        spans.append((max(1, start), end))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(
        ContextBlock(
            kind=ContextBlockKind.WINDOW,
            name="",
            start_line=start,
            end_line=end,
            text=_slice(source_lines, start, end),
        )
        for start, end in merged
    )


def _symbol_blocks(
    spans: Sequence[SymbolSpan], source_lines: Sequence[str], *, signature_only: bool
) -> tuple[ContextBlock, ...]:
    """Tier 1 keeps the whole symbol; tier 2 stops at the signature/docstring."""
    blocks = []
    for span in spans:
        end = span.signature_end_line if signature_only else span.end_line
        if span.start_line > len(source_lines) or end < span.start_line:
            continue
        blocks.append(
            ContextBlock(
                kind=ContextBlockKind.CALLED if signature_only else ContextBlockKind.ENCLOSING,
                name=span.name,
                start_line=span.start_line,
                end_line=end,
                text=_slice(source_lines, span.start_line, end),
            )
        )
    return tuple(blocks)


@dataclass(frozen=True, slots=True)
class _FileBlocks:
    """One file's candidate blocks, before the budget decides what survives."""

    path: str
    tier1: tuple[ContextBlock, ...]
    tier2: tuple[ContextBlock, ...]
    fallback: tuple[ContextBlock, ...]


def build_context(
    files: Sequence[FileDiff],
    sources: Mapping[str, str],
    index: SymbolIndex,
    *,
    token_budget: int,
    estimate: Callable[[str], int],
    fallback_pad: int = FALLBACK_PAD_LINES,
) -> tuple[tuple[FileContext, ...], ContextMetrics]:
    """Tier 1/2 blocks per file ([C1]), fallback windows when the AST is not
    available ([C2]), then the budget cut ([C4]): all of tier 2, tier 1 file by
    file from the end of the diff order, then the fallback windows.

    A file whose source was not fetched (past the caps of §1.3) gets no block at
    all: its hunk lines, already in the diff, are its context. The returned
    metrics describe the surviving context only.
    """
    candidates: list[_FileBlocks] = []
    parse_errors = 0

    for diff in files:
        source = sources.get(diff.new_path)
        if source is None:
            continue
        source_lines = source.split("\n")
        anchors = _anchor_lines(diff)
        if not anchors:
            continue
        if not index.supports(diff.new_path):
            candidates.append(
                _FileBlocks(
                    diff.new_path, (), (), _fallback_windows(diff, source_lines, pad=fallback_pad)
                )
            )
            continue
        try:
            enclosing_spans = index.enclosing(diff.new_path, source, anchors)
            called_spans = [
                span
                for span in index.called_from(diff.new_path, source, anchors)
                # A recursive call of the enclosing symbol is already tier 1 in full.
                if (span.name, span.start_line)
                not in {(e.name, e.start_line) for e in enclosing_spans}
            ]
            tier1 = _symbol_blocks(
                enclosing_spans,
                source_lines,
                signature_only=False,
            )
            tier2 = _symbol_blocks(
                called_spans,
                source_lines,
                signature_only=True,
            )
        except SymbolParseError:
            parse_errors += 1
            candidates.append(
                _FileBlocks(
                    diff.new_path, (), (), _fallback_windows(diff, source_lines, pad=fallback_pad)
                )
            )
            continue
        if not tier1 and not tier2:
            # Nothing enclosing at module level: a window is still better context.
            candidates.append(
                _FileBlocks(
                    diff.new_path, (), (), _fallback_windows(diff, source_lines, pad=fallback_pad)
                )
            )
            continue
        candidates.append(_FileBlocks(diff.new_path, tier1, tier2, ()))

    cut = ContextCut.NONE
    surviving = list(candidates)

    def _cost(blocks: _FileBlocks) -> int:
        return sum(
            estimate(block.text) for block in (*blocks.tier1, *blocks.tier2, *blocks.fallback)
        )

    def _total(all_blocks: Sequence[_FileBlocks]) -> int:
        return sum(_cost(blocks) for blocks in all_blocks)

    if _total(surviving) > token_budget:
        cut = ContextCut.TIER2
        surviving = [_FileBlocks(b.path, b.tier1, (), b.fallback) for b in surviving]
    if _total(surviving) > token_budget:
        cut = ContextCut.TIER1
        # Tier 1 goes file by file from the end of the diff order ([C4]).
        while _total(surviving) > token_budget:
            tier1_positions = [i for i, b in enumerate(surviving) if b.tier1]
            if not tier1_positions:
                break
            at = tier1_positions[-1]
            stripped = _FileBlocks(surviving[at].path, (), (), surviving[at].fallback)
            if stripped.tier2 or stripped.fallback:
                surviving[at] = stripped
            else:
                surviving.pop(at)
    if _total(surviving) > token_budget:
        cut = ContextCut.FALLBACK
        # The fallback windows go last; past them, whole files from the end.
        while _total(surviving) > token_budget and surviving:
            last = surviving[-1]
            if last.fallback:
                surviving[-1] = _FileBlocks(last.path, last.tier1, last.tier2, ())
            else:
                surviving.pop()

    contexts = tuple(
        FileContext(path=b.path, blocks=(*b.tier1, *b.tier2, *b.fallback))
        for b in surviving
        if b.tier1 or b.tier2 or b.fallback
    )
    tier1_symbols = sum(
        1
        for context in contexts
        for block in context.blocks
        if block.kind is ContextBlockKind.ENCLOSING
    )
    tier2_symbols = sum(
        1
        for context in contexts
        for block in context.blocks
        if block.kind is ContextBlockKind.CALLED
    )
    kept_fallback_files = sum(
        1
        for context in contexts
        if context.blocks and all(block.kind is ContextBlockKind.WINDOW for block in context.blocks)
    )
    metrics = ContextMetrics(
        context_files=len(contexts),
        tier1_symbols=tier1_symbols,
        tier2_symbols=tier2_symbols,
        fallback_files=kept_fallback_files,
        parse_errors=parse_errors,
        context_tokens=sum(
            estimate(block.text) for context in contexts for block in context.blocks
        ),
        context_cut=cut,
    )
    return contexts, metrics
