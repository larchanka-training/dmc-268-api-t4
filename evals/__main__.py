"""python -m evals validate|run: check the gold dataset, or score it with a live model."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from adapters.llm.config import LLMSettings
from domain.errors import LLMConfigurationError
from evals.loader import DatasetError, load_dataset
from evals.models import Case
from evals.report import render_summary, results_json
from evals.runner import LiveRunner, RunRecord
from evals.scoring import RunStatus

DEFAULT_DATASET = Path("test-prs-dataset")
EXIT_NOTHING_MEASURED = 1
EXIT_BAD_INPUT = 2


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="load the dataset; no network, no keys")
    validate.add_argument("dataset", nargs="?", type=Path, default=DEFAULT_DATASET)

    run = commands.add_parser("run", help="review every case with the live model and score it")
    run.add_argument("dataset", nargs="?", type=Path, default=DEFAULT_DATASET)
    run.add_argument("--only", action="append", default=[], metavar="ID", help="run this case only")
    run.add_argument("--summary", type=Path, help="write the Markdown summary here")
    run.add_argument("--results", type=Path, help="write the keys-only JSON results here")
    return parser.parse_args(argv)


def _load(dataset: Path) -> list[Case] | None:
    try:
        return load_dataset(dataset)
    except (DatasetError, OSError) as exc:
        print(f"dataset error: {exc}", file=sys.stderr)
        return None


async def _run_all(runner: LiveRunner, cases: list[Case]) -> list[RunRecord]:
    records: list[RunRecord] = []
    for index, case in enumerate(cases, start=1):
        record = await runner.run(case)
        records.append(record)
        print(f"[{index}/{len(cases)}] {case.id}: {record.score.status.value}", file=sys.stderr)
    return records


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    cases = _load(args.dataset)
    if cases is None:
        return EXIT_BAD_INPUT
    if args.command == "validate":
        print(f"{len(cases)} cases valid")
        return 0

    if args.only:
        unknown = sorted(set(args.only) - {case.id for case in cases})
        if unknown:
            print(f"unknown case id(s): {', '.join(unknown)}", file=sys.stderr)
            return EXIT_BAD_INPUT
        cases = [case for case in cases if case.id in args.only]
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        runner = LiveRunner(LLMSettings.from_env())
    except LLMConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    records = asyncio.run(_run_all(runner, cases))
    summary = render_summary(records)
    print(summary)
    if args.summary:
        args.summary.write_text(summary, encoding="utf-8")
    if args.results:
        args.results.write_text(
            json.dumps(results_json(records), indent=2) + "\n", encoding="utf-8"
        )
    # Report-only: a low score is not a failure. Nothing measured at all is.
    measured = any(record.score.status is not RunStatus.ERRORED for record in records)
    return 0 if measured else EXIT_NOTHING_MEASURED


if __name__ == "__main__":
    sys.exit(main())
