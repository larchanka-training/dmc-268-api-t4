"""Manual smoke check against a live provider. Never run from tests or CI."""

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from adapters.context import TreeSitterSymbolIndex
from adapters.llm.config import CONTEXT_TOKEN_BUDGET_FRACTION, LLMSettings
from adapters.llm.factory import build_gateway
from adapters.llm.prompt import estimate_text_tokens
from domain.context import MAX_SOURCE_BYTES, build_context, select_context_files
from domain.diff import parse_diff
from domain.diff_filter import filter_files
from domain.errors import LLMConfigurationError, LLMGatewayError
from domain.models import ReviewResult
from domain.ports import FileContext, ReviewRequest
from domain.publishing import CommentableLines, build_plan

EXIT_CONFIG_ERROR = 2
EXIT_REVIEW_FAILED = 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m adapters.llm.smoke",
        description="Send one diff through the LLM gateway and print the findings as JSON.",
    )
    parser.add_argument("diff", type=Path, help="path to a unified diff file")
    parser.add_argument("--title", default="Smoke test", help="pull request title")
    parser.add_argument("--description", default="", help="pull request description")
    parser.add_argument(
        "--rules", type=Path, default=None, help="repository rules file (.review/rules.md)"
    )
    parser.add_argument(
        "--context",
        type=Path,
        default=None,
        help="a checkout of the pull request's head revision: AST context is built "
        "from the diff's files read under this directory",
    )
    parser.add_argument(
        "--check-run",
        type=Path,
        default=None,
        help="also write the Check Run summary the worker would publish to this file",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = LLMSettings.from_env()
    except LLMConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    try:
        diff_text = args.diff.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read diff file {args.diff}: {exc.strerror}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    rules_markdown = None
    if args.rules is not None:
        try:
            rules_markdown = args.rules.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"cannot read rules file {args.rules}: {exc.strerror}", file=sys.stderr)
            return EXIT_CONFIG_ERROR

    request = ReviewRequest(
        diff_text=diff_text,
        pr_title=args.title,
        pr_description=args.description,
        rules_markdown=rules_markdown,
        context=load_context(
            diff_text,
            args.context,
            budget_tokens=int(settings.max_input_tokens * CONTEXT_TOKEN_BUDGET_FRACTION),
        ),
    )
    try:
        result = asyncio.run(build_gateway(settings).review(request))
    except LLMGatewayError as exc:
        print(f"review failed: {exc}", file=sys.stderr)
        return EXIT_REVIEW_FAILED
    print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
    if args.check_run is not None:
        args.check_run.write_text(check_run_summary(result, diff_text), encoding="utf-8")
    return 0


def load_context(
    diff_text: str, checkout: Path | None, *, budget_tokens: int
) -> tuple[FileContext, ...]:
    """The same context the worker's BUILD_CONTEXT step builds, from a local
    checkout instead of the forge: sources are read by their repo paths under
    `checkout` (a missing or oversized file simply gets no context)."""
    if checkout is None:
        return ()
    selected = select_context_files(filter_files(parse_diff(diff_text)).kept)
    sources: dict[str, str] = {}
    for diff in selected:
        path = checkout / diff.new_path
        if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
            continue
        try:
            sources[diff.new_path] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
    contexts, _metrics = build_context(
        selected,
        sources,
        TreeSitterSymbolIndex(),
        token_budget=budget_tokens,
        estimate=estimate_text_tokens,
    )
    return contexts


def check_run_summary(result: ReviewResult, diff_text: str) -> str:
    """The Check Run summary, built the way worker/process.py builds it (default policy)."""
    plan = build_plan(
        result.findings,
        model_summary=result.summary,
        lines=CommentableLines.from_files(filter_files(parse_diff(diff_text)).kept),
        model_risk=result.risk,
        checklist=result.checklist,
        dismissed_by_verifier=result.rejected_by_verifier,
    )
    return plan.summary


if __name__ == "__main__":
    sys.exit(main())
