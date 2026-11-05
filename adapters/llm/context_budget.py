"""Fit optional review context into the budget of the rendered LLM prompt."""

from collections.abc import Sequence
from dataclasses import dataclass, replace

from adapters.llm.config import CONTEXT_TOKEN_BUDGET_FRACTION, ReviewScope
from adapters.llm.prompt import PROMPT_VERSION, build_messages, estimate_tokens
from adapters.llm.rules import Rule
from domain.context import ContextCut
from domain.diff import parse_diff, split_by_file
from domain.errors import DiffFormatError
from domain.ports import ContextBlockKind, FileContext, PromptFit, ReviewRequest


@dataclass(frozen=True, slots=True)
class ContextBudgetFit(PromptFit):
    """The request after optional context was cut to fit the rendered prompt.

    A `domain.ports.PromptFit`: the worker computes it once and passes it to the
    gateway, which reuses it instead of trimming again.
    """

    request: ReviewRequest
    estimated_tokens: int
    context_tokens: int
    context_cut: ContextCut


def fit_context_to_prompt(
    request: ReviewRequest,
    rules: Sequence[Rule],
    *,
    max_input_tokens: int,
    prompt_version: str = PROMPT_VERSION,
    review_scope: ReviewScope = ReviewScope.PR,
) -> ContextBudgetFit:
    """Trim context until both its share and the complete prompt fit.

    The prompt is measured after serialization, so line numbers, labels, markers,
    rules and the diff all count. The diff and other non-context input are never
    shortened. Context is cut in spec order: called signatures, enclosing symbols
    from the last file backwards, then fallback windows.
    """

    contexts = request.context
    cut = ContextCut.NONE
    context_limit = int(max_input_tokens * CONTEXT_TOKEN_BUDGET_FRACTION)

    def estimate_parts(
        candidate: tuple[FileContext, ...],
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        full_request = replace(request, context=candidate)
        base_request = replace(full_request, context=())
        full_parts = split_request(full_request, review_scope)
        base_parts = split_request(base_request, review_scope)
        full = tuple(
            estimate_tokens(build_messages(part, rules, version=prompt_version))
            for part in full_parts
        )
        base = tuple(
            estimate_tokens(build_messages(part, rules, version=prompt_version))
            for part in base_parts
        )
        return full, base

    def fit(candidate: tuple[FileContext, ...]) -> tuple[bool, int, int]:
        full, base = estimate_parts(candidate)
        estimated = max(full, default=0)
        context_tokens = max(
            (total - plain for total, plain in zip(full, base, strict=True)), default=0
        )
        return (
            estimated <= max_input_tokens and context_tokens <= context_limit,
            estimated,
            context_tokens,
        )

    fits, estimated, context_tokens = fit(contexts)
    if fits:
        return ContextBudgetFit(request, estimated, context_tokens, cut)

    # All tier-2 signatures are less important than any enclosing symbol.
    without_called = _without_kind(contexts, ContextBlockKind.CALLED)
    if without_called != contexts:
        contexts = without_called
        cut = ContextCut.TIER2
        fits, estimated, context_tokens = fit(contexts)
        if fits:
            return _result(request, contexts, estimated, context_tokens, cut)

    # Remove enclosing symbols file by file from the end of the diff order.
    for index in range(len(contexts) - 1, -1, -1):
        without_enclosing = _without_kind_from_file(contexts, index, ContextBlockKind.ENCLOSING)
        if without_enclosing == contexts:
            continue
        contexts = without_enclosing
        cut = ContextCut.TIER1
        fits, estimated, context_tokens = fit(contexts)
        if fits:
            return _result(request, contexts, estimated, context_tokens, cut)

    # Fallback windows are last; preserve earlier files for as long as possible.
    for index in range(len(contexts) - 1, -1, -1):
        without_window = _without_kind_from_file(contexts, index, ContextBlockKind.WINDOW)
        if without_window == contexts:
            continue
        contexts = without_window
        cut = ContextCut.FALLBACK
        fits, estimated, context_tokens = fit(contexts)
        if fits:
            return _result(request, contexts, estimated, context_tokens, cut)

    # Defensive fallback for a future block kind: never let optional context
    # make a prompt fail when its non-context portion fits.
    while contexts:
        contexts = contexts[:-1]
        cut = ContextCut.FALLBACK
        fits, estimated, context_tokens = fit(contexts)
        if fits:
            return _result(request, contexts, estimated, context_tokens, cut)

    return _result(request, (), estimated, context_tokens, cut)


def _result(
    request: ReviewRequest,
    contexts: tuple[FileContext, ...],
    estimated: int,
    context_tokens: int,
    cut: ContextCut,
) -> ContextBudgetFit:
    return ContextBudgetFit(
        request=replace(request, context=contexts),
        estimated_tokens=estimated,
        context_tokens=context_tokens,
        context_cut=cut,
    )


def split_request(request: ReviewRequest, scope: ReviewScope) -> tuple[ReviewRequest, ...]:
    """Split a request into first-pass parts: one per file in FILE scope.

    [C4]'s cap is per request, so a per-file part carries only its own file's
    context blocks — the full `request.context` on every part would spend
    n_files × context. A part whose path cannot be parsed keeps no context:
    its hunks, already in its diff, are its context. The summary call and PR
    scope keep the whole request, context included, exactly once.
    """
    if scope is ReviewScope.PR:
        return (request,)
    parts = tuple(
        replace(request, diff_text=file_diff, context=_own_context(file_diff, request.context))
        for file_diff in split_by_file(request.diff_text)
    )
    return parts or (request,)


def _own_context(file_diff: str, contexts: Sequence[FileContext]) -> tuple[FileContext, ...]:
    """The context blocks of the one file `file_diff` describes."""
    try:
        paths = {
            path
            for diff in parse_diff(file_diff)
            if (path := diff.new_path or diff.old_path) is not None
        }
    except DiffFormatError:
        return ()
    return tuple(context for context in contexts if context.path in paths)


def _without_kind(
    contexts: tuple[FileContext, ...], kind: ContextBlockKind
) -> tuple[FileContext, ...]:
    result = []
    for context in contexts:
        blocks = tuple(block for block in context.blocks if block.kind is not kind)
        if blocks:
            result.append(replace(context, blocks=blocks))
    return tuple(result)


def _without_kind_from_file(
    contexts: tuple[FileContext, ...], index: int, kind: ContextBlockKind
) -> tuple[FileContext, ...]:
    context = contexts[index]
    blocks = tuple(block for block in context.blocks if block.kind is not kind)
    if blocks == context.blocks:
        return contexts
    updated = list(contexts)
    if blocks:
        updated[index] = replace(context, blocks=blocks)
    else:
        del updated[index]
    return tuple(updated)
