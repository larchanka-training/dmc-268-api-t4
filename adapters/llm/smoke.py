"""Manual smoke check against a live provider. Never run from tests or CI.

With SENTRY_DSN set, the review is one ``llm.smoke`` transaction, so the gateway's
``llm.*`` spans reach Sentry (docs/observability.md § Review pipeline); ENV names the
environment, ``smoke`` in llm-smoke.yml.
"""

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

import sentry_sdk

from adapters.llm.config import LLMSettings
from adapters.llm.factory import build_gateway
from adapters.observability.config import SentrySettings
from adapters.observability.sentry import init_sentry
from domain.diff import parse_diff
from domain.diff_filter import filter_files
from domain.errors import LLMConfigurationError, LLMGatewayError
from domain.models import ReviewResult
from domain.ports import ReviewRequest
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
        "--check-run",
        type=Path,
        default=None,
        help="also write the Check Run summary the worker would publish to this file",
    )
    return parser.parse_args(argv)


async def _review(settings: LLMSettings, request: ReviewRequest) -> ReviewResult:
    # A transaction to hang the gateway's spans on; a no-op without a DSN.
    with sentry_sdk.start_transaction(name="llm.smoke", op="llm.smoke"):
        return await build_gateway(settings).review(request)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    init_sentry(SentrySettings.from_env())
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
    )
    try:
        result = asyncio.run(_review(settings, request))
    except LLMGatewayError as exc:
        print(f"review failed: {exc}", file=sys.stderr)
        return EXIT_REVIEW_FAILED
    finally:
        sentry_sdk.flush()
    print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
    if args.check_run is not None:
        args.check_run.write_text(check_run_summary(result, diff_text), encoding="utf-8")
    return 0


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
