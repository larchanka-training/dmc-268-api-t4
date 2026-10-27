import json
from pathlib import Path

import httpx
import pytest

import evals.__main__ as cli
from adapters.llm.config import LLMSettings
from evals.runner import LiveRunner
from tests.conftest import FAKE_KEY_A, VALID_FINDING, chat_response, review_json

DATASET = Path(__file__).resolve().parents[2] / "test-prs-dataset"
ENV = {"LLM_PRIMARY_API_KEYS": FAKE_KEY_A, "LLM_PRIMARY_MODEL": "m"}


def test_validate_accepts_the_committed_dataset(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["validate", str(DATASET)]) == 0
    assert "cases valid" in capsys.readouterr().out


def test_validate_rejects_a_missing_dataset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["validate", str(tmp_path / "nope")]) == cli.EXIT_BAD_INPUT
    assert "dataset error" in capsys.readouterr().err


def test_run_without_keys_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("LLM_PRIMARY_API_KEYS", "LLM_PRIMARY_MODEL"):
        monkeypatch.delenv(name, raising=False)

    assert cli.main(["run", str(DATASET)]) == cli.EXIT_BAD_INPUT
    assert "LLM_PRIMARY_API_KEYS" in capsys.readouterr().err


def test_run_rejects_an_unknown_case_id(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["run", str(DATASET), "--only", "nope"]) == cli.EXIT_BAD_INPUT
    assert "unknown case id" in capsys.readouterr().err


def set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)


def fake_runner(*responses: httpx.Response) -> LiveRunner:
    queue = list(responses)
    transport = httpx.MockTransport(lambda request: queue.pop(0))
    return LiveRunner(LLMSettings.from_env(ENV), transport=transport)


def test_run_writes_a_summary_and_keys_only_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = fake_runner(chat_response(review_json(VALID_FINDING)))
    monkeypatch.setattr(cli, "LiveRunner", lambda settings: runner)
    set_env(monkeypatch)
    summary, results = tmp_path / "summary.md", tmp_path / "results.json"

    code = cli.main(
        [
            "run",
            str(DATASET),
            "--only",
            "sec-sql-sort-injection",
            "--summary",
            str(summary),
            "--results",
            str(results),
        ]
    )

    assert code == 0
    text = summary.read_text(encoding="utf-8")
    assert "schema-valid: **100.0%**" in text
    assert "| sec-sql-sort-injection | detection | ok | 1 | 0 | 0 |" in text
    payload = json.loads(results.read_text(encoding="utf-8"))
    assert payload["cases"][0]["findings"] == [
        {
            "path": "app/users.py",
            "line": 16,
            "side": "new",
            "category": "security",
            "severity": "critical",
            "verdict": "tp",
        }
    ]
    assert VALID_FINDING["message"] not in text + results.read_text(encoding="utf-8")
    assert VALID_FINDING["message"] not in capsys.readouterr().out


def test_run_exits_nonzero_when_nothing_could_be_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = fake_runner(httpx.Response(503))
    monkeypatch.setattr(cli, "LiveRunner", lambda settings: runner)
    set_env(monkeypatch)

    assert (
        cli.main(["run", str(DATASET), "--only", "sec-sql-sort-injection"])
        == cli.EXIT_NOTHING_MEASURED
    )


def test_context_manifest_is_validated_and_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    manifest = tmp_path / "context.json"
    manifest.write_text(
        json.dumps({"sec-sql-sort-injection": {"checkout": "checkout"}}), encoding="utf-8"
    )
    runner = fake_runner(chat_response(review_json(VALID_FINDING)))
    monkeypatch.setattr(cli, "LiveRunner", lambda settings, **kwargs: runner)
    set_env(monkeypatch)

    result = cli.main(
        [
            "run",
            str(DATASET),
            "--only",
            "sec-sql-sort-injection",
            "--context-manifest",
            str(manifest),
        ]
    )

    assert result == 0
    assert "context enabled" in capsys.readouterr().out


def test_context_manifest_missing_paths_are_clear_input_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "context.json"
    manifest.write_text(
        json.dumps({"sec-sql-sort-injection": {"checkout": "missing"}}), encoding="utf-8"
    )

    assert (
        cli.main(
            [
                "run",
                str(DATASET),
                "--only",
                "sec-sql-sort-injection",
                "--context-manifest",
                str(manifest),
            ]
        )
        == cli.EXIT_BAD_INPUT
    )
    assert "context manifest error" in capsys.readouterr().err
