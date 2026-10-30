"""Reading a compare diff against a mock transport. No test opens a connection."""

import httpx
import pytest

from adapters.github.app_auth import GitHubAppAuth
from adapters.github.compare import GitHubCompareDiffs
from domain.errors import ForgeRateLimitedError, ForgeUnavailableError, InstallationGoneError
from tests.adapters.test_github_contents import settings
from tests.conftest import FixedClock, RecordingTransport, github_fixture

DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n"
BASE = "b" * 40
HEAD = "h" * 40


def diffs_for(
    clock: FixedClock, response: httpx.Response
) -> tuple[GitHubCompareDiffs, RecordingTransport]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json=github_fixture("installation_token"))
        return response

    transport = RecordingTransport(handler)
    config = settings()
    auth = GitHubAppAuth(config, clock, transport=transport)
    return GitHubCompareDiffs(config, auth, transport=transport), transport


async def test_the_three_dot_diff_is_returned(auth_clock: FixedClock) -> None:
    diffs, transport = diffs_for(auth_clock, httpx.Response(200, text=DIFF))

    assert await diffs.compare_diff(42, "acme/payments", BASE, HEAD) == DIFF

    request = transport.requests[-1]
    assert request.url.path == f"/repos/acme/payments/compare/{BASE}...{HEAD}"
    assert request.headers["accept"] == "application/vnd.github.diff"
    assert request.headers["authorization"].startswith("Bearer ")


@pytest.mark.parametrize("status", [404, 422])
async def test_a_gone_commit_is_none(auth_clock: FixedClock, status: int) -> None:
    diffs, _ = diffs_for(auth_clock, httpx.Response(status, json={"message": "No commit"}))

    assert await diffs.compare_diff(42, "acme/payments", BASE, HEAD) is None


async def test_a_403_is_a_gone_installation(auth_clock: FixedClock) -> None:
    diffs, _ = diffs_for(auth_clock, httpx.Response(403, json={"message": "secret body"}))

    with pytest.raises(InstallationGoneError) as caught:
        await diffs.compare_diff(42, "acme/payments", BASE, HEAD)
    assert "secret body" not in str(caught.value)


async def test_rate_limit_and_server_errors_are_transient(auth_clock: FixedClock) -> None:
    limited, _ = diffs_for(auth_clock, httpx.Response(429, headers={"retry-after": "30"}))
    down, _ = diffs_for(auth_clock, httpx.Response(502))

    with pytest.raises(ForgeRateLimitedError):
        await limited.compare_diff(42, "acme/payments", BASE, HEAD)
    with pytest.raises(ForgeUnavailableError):
        await down.compare_diff(42, "acme/payments", BASE, HEAD)


async def test_a_malformed_repo_name_never_reaches_the_network(auth_clock: FixedClock) -> None:
    diffs, transport = diffs_for(auth_clock, httpx.Response(200, text=DIFF))

    with pytest.raises(ForgeUnavailableError):
        await diffs.compare_diff(42, "acme/pay/ments", BASE, HEAD)
    assert transport.requests == []
