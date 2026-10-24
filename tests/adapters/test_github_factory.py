"""The factory wires settings into a GitProvider without reaching the network."""

from pathlib import Path

import httpx

from adapters.github.config import GitHubAppSettings
from adapters.github.factory import build_vcs_client
from tests.conftest import FixedClock

KEY_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "github" / "app_key.pem"


def settings() -> GitHubAppSettings:
    return GitHubAppSettings(
        app_id="123456",
        private_key=KEY_PATH.read_text(encoding="utf-8"),
        app_slug="review-agent",
        webhook_secret="whsec_test_secret",
        api_base_url="https://api.github.test",
    )


def test_build_vcs_client_returns_a_git_provider() -> None:
    provider = build_vcs_client(settings())

    assert hasattr(provider, "fetch_pull_request")
    assert callable(provider.fetch_pull_request)


def test_build_vcs_client_accepts_transport_and_clock() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(404))

    provider = build_vcs_client(settings(), transport=transport, clock=FixedClock())

    assert callable(provider.fetch_pull_request)
