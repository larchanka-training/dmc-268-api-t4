"""Wiring for the GitHub VCS client, mirroring adapters/llm/factory.py."""

import httpx

from adapters.clock import SystemClock
from adapters.github.app_auth import GitHubAppAuth
from adapters.github.client import GitHubVCSClient
from adapters.github.config import GitHubAppSettings
from domain.ports import Clock, GitProvider


def build_vcs_client(
    settings: GitHubAppSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock | None = None,
) -> GitProvider:
    app_auth = GitHubAppAuth(
        settings, clock if clock is not None else SystemClock(), transport=transport
    )
    return GitHubVCSClient(settings, app_auth, transport=transport)
