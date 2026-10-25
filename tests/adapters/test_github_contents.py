"""Reading a repository file against a mock transport. No test opens a connection."""

from pathlib import Path

import httpx
import pytest

from adapters.github.app_auth import GitHubAppAuth
from adapters.github.config import GitHubAppSettings
from adapters.github.contents import GitHubRepositoryContents
from domain.errors import (
    ForgePermissionError,
    ForgeRateLimitedError,
    ForgeUnavailableError,
    InstallationGoneError,
)
from tests.conftest import FixedClock, RecordingTransport, github_fixture

KEY_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "github" / "app_key.pem"
RULES = b"# Security\n- Bind every SQL parameter.\n"


def settings() -> GitHubAppSettings:
    return GitHubAppSettings(
        app_id="123456",
        private_key=KEY_PATH.read_text(encoding="utf-8"),
        app_slug="review-agent",
        webhook_secret="whsec_test",
        api_base_url="https://api.github.test",
    )


def contents_for(
    clock: FixedClock, response: httpx.Response
) -> tuple[GitHubRepositoryContents, RecordingTransport]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json=github_fixture("installation_token"))
        return response

    transport = RecordingTransport(handler)
    config = settings()
    auth = GitHubAppAuth(config, clock, transport=transport)
    return GitHubRepositoryContents(config, auth, transport=transport), transport


async def read(contents: GitHubRepositoryContents, max_bytes: int = 1024) -> bytes | None:
    return await contents.read_file(
        42, "acme/payments", ".review/rules.md", "feature/x", max_bytes=max_bytes
    )


async def test_the_raw_file_is_returned(auth_clock: FixedClock) -> None:
    contents, transport = contents_for(auth_clock, httpx.Response(200, content=RULES))

    assert await read(contents) == RULES

    request = transport.requests[-1]
    assert request.url.path == "/repos/acme/payments/contents/.review/rules.md"
    assert request.url.params["ref"] == "feature/x"
    assert request.headers["accept"] == "application/vnd.github.raw+json"
    assert request.headers["authorization"].startswith("Bearer ")


async def test_a_missing_file_is_none(auth_clock: FixedClock) -> None:
    contents, _ = contents_for(auth_clock, httpx.Response(404, json={"message": "Not Found"}))

    assert await read(contents) is None


async def test_an_oversized_file_is_cut_one_byte_past_the_limit(auth_clock: FixedClock) -> None:
    contents, _ = contents_for(auth_clock, httpx.Response(200, content=b"x" * 5000))

    body = await read(contents, max_bytes=100)

    assert body == b"x" * 101


async def test_a_403_is_a_permission_error(auth_clock: FixedClock) -> None:
    contents, _ = contents_for(
        auth_clock, httpx.Response(403, json={"message": "Resource not accessible"})
    )

    with pytest.raises(ForgePermissionError) as caught:
        await read(contents)
    assert "Resource" not in str(caught.value)


async def test_a_403_with_no_rate_left_is_rate_limited(auth_clock: FixedClock) -> None:
    contents, _ = contents_for(
        auth_clock,
        httpx.Response(403, headers={"x-ratelimit-remaining": "0", "retry-after": "30"}),
    )

    with pytest.raises(ForgeRateLimitedError) as caught:
        await read(contents)
    assert caught.value.retry_after_seconds == 30


async def test_a_server_error_is_forge_unavailable(auth_clock: FixedClock) -> None:
    contents, _ = contents_for(auth_clock, httpx.Response(502, text="secret body"))

    with pytest.raises(ForgeUnavailableError) as caught:
        await read(contents)
    assert "secret body" not in str(caught.value)


async def test_a_network_error_is_forge_unavailable(auth_clock: FixedClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json=github_fixture("installation_token"))
        raise httpx.ConnectError("refused", request=request)

    transport = RecordingTransport(handler)
    config = settings()
    contents = GitHubRepositoryContents(
        config, GitHubAppAuth(config, auth_clock, transport=transport), transport=transport
    )

    with pytest.raises(ForgeUnavailableError):
        await read(contents)


async def test_a_gone_installation_surfaces_from_the_token_call(auth_clock: FixedClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    transport = RecordingTransport(handler)
    config = settings()
    contents = GitHubRepositoryContents(
        config, GitHubAppAuth(config, auth_clock, transport=transport), transport=transport
    )

    with pytest.raises(InstallationGoneError):
        await read(contents)


async def test_the_file_content_is_never_logged(
    auth_clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    contents, _ = contents_for(auth_clock, httpx.Response(200, content=RULES))

    with caplog.at_level("DEBUG"):
        await read(contents)

    assert "Bind every SQL" not in caplog.text
    for record in caplog.records:
        assert "Bind every SQL" not in str(record.__dict__)
