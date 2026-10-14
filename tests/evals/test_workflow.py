"""The eval workflow is the live stand: manual, serialized with the smoke job, never on a PR."""

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def load(name: str) -> dict:
    loaded = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_llm_eval_runs_only_on_manual_dispatch() -> None:
    triggers = load("llm-eval.yml")[True]  # YAML 1.1 reads the key `on` as True

    assert set(triggers) == {"workflow_dispatch"}


def test_llm_eval_shares_the_smoke_concurrency_group_without_cancelling() -> None:
    eval_group = load("llm-eval.yml")["concurrency"]
    smoke_group = load("llm-smoke.yml")["concurrency"]

    assert eval_group == smoke_group
    assert eval_group["cancel-in-progress"] is False


def test_ci_never_receives_the_llm_key() -> None:
    assert "AI_DMC268_T4" not in (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
