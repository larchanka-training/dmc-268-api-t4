"""Manual smoke check against a live provider. Never run from tests or CI."""

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from adapters.llm.config import LLMSettings
from adapters.llm.factory import build_gateway
from domain.errors import LLMConfigurationError, LLMGatewayError
from domain.ports import ReviewRequest

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

    request = ReviewRequest(
        diff_text=diff_text, pr_title=args.title, pr_description=args.description
    )
    try:
        result = asyncio.run(build_gateway(settings).review(request))
    except LLMGatewayError as exc:
        print(f"review failed: {exc}", file=sys.stderr)
        return EXIT_REVIEW_FAILED
    print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
