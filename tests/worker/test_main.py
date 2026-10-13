"""``python -m worker``: import safety and the entrypoint wiring.

The module must be importable without side effects (the ``__main__`` guard
keeps ``main()`` from running on import), and the VCS wiring must mirror
``api.main.build_app``.
"""

import importlib
from pathlib import Path

from adapters.github.client import GitHubVCSClient

KEY_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "github" / "app_key.pem"


def _github_env(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(KEY_PATH))
    monkeypatch.setenv("GITHUB_APP_SLUG", "review-agent")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "whsec_test_secret_value")


def test_the_entrypoint_module_imports_without_running(monkeypatch) -> None:
    # Stub the GitHub env so any accidental import-time construction
    # would at least find it; the point is that nothing runs.
    _github_env(monkeypatch)

    module = importlib.import_module("worker.__main__")

    assert callable(module.main)


def test_build_vcs_from_env_mirrors_the_api_wiring(monkeypatch) -> None:
    from worker.__main__ import build_vcs_from_env

    _github_env(monkeypatch)

    assert isinstance(build_vcs_from_env(), GitHubVCSClient)
