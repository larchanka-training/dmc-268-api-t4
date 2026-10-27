import ast
import json
from pathlib import Path

from adapters.context import TreeSitterSymbolIndex
from domain.context import build_context, select_context_files
from domain.diff import parse_diff
from domain.diff_filter import filter_files
from evals.context import load_context_manifest
from evals.loader import load_dataset

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "test-prs-dataset"
MANIFEST = DATASET / "eval-context.json"


def test_every_dataset_case_has_usable_offline_context_fixtures() -> None:
    cases = load_dataset(DATASET)
    sources = load_context_manifest(MANIFEST, {case.id for case in cases})
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert len(manifest) == 29

    for case in cases:
        source = sources[case.id]
        assert source.checkout is not None and source.checkout.is_dir()
        assert source.rules == (ROOT / ".review/rules.md").resolve()
        assert source.rules.is_file()

        parsed = parse_diff(case.read_diff())
        checkout_files = {
            file_diff.new_path for file_diff in select_context_files(filter_files(parsed).kept)
        }
        assert checkout_files
        for relative in checkout_files:
            path = source.checkout / relative
            assert path.is_file(), f"{case.id}: missing {relative}"
            text = path.read_text(encoding="utf-8")
            ast.parse(text, filename=str(path))

        selected = select_context_files(filter_files(parsed).kept)
        checkout_text = {
            file_diff.new_path: (source.checkout / file_diff.new_path).read_text(encoding="utf-8")
            for file_diff in selected
        }
        contexts, metrics = build_context(
            selected,
            checkout_text,
            TreeSitterSymbolIndex(),
            token_budget=20_000,
            estimate=lambda text: len(text),
        )
        assert contexts, case.id
        assert metrics.tier1_symbols > 0, case.id
        assert all(context.blocks for context in contexts)
