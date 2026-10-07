"""Repository listing against recorded pages. No test opens a connection."""

from pathlib import Path

import httpx
import pytest

from adapters.github.app_auth import GitHubAppAuth
from adapters.github.config import GitHubAppSettings
from adapters.github.installation import MAX_PAGES, PER_PAGE, GitHubInstallationGateway
from domain.errors import ForgeUnavailableError, InstallationGoneError
from tests.conftest import FixedClock, RecordingTransport, github_fixture

KEY_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "github" / "app_key.pem"
TOKEN = "ghs_test_aaaaaaaaaaaaaaaaaaaaaaaa"


def settings() -> GitHubAppSettings:
    return GitHubAppSettings(
        app_id="123456",
        private_key=KEY_PATH.read_text(encoding="utf-8"),
        app_slug="review-agent",
        webhook_secret="whsec_test",
        api_base_url="https://api.github.test",
    )


def gateway_for(clock: FixedClock, handler: object) -> tuple[GitHubInstallationGateway, object]:
    transport = RecordingTransport(handler)  # type: ignore[arg-type]
    config = settings()
    auth = GitHubAppAuth(config, clock, transport=transport)
    return GitHubInstallationGateway(config, auth, transport=transport), transport


def routed(repositories_status: int = 200, body: object | None = None) -> object:
    """Serves the token endpoint and the listing endpoint from one handler."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json=github_fixture("installation_token"))
        if repositories_status != 200:
            return httpx.Response(repositories_status, json={"message": "nope"})
        return httpx.Response(
            200, json=body if body is not None else github_fixture("installation_repositories")
        )

    return handler


async def test_repositories_are_mapped_from_the_recorded_page(auth_clock: FixedClock) -> None:
    gateway, _ = gateway_for(auth_clock, routed())

    repositories = await gateway.list_repositories(42)

    by_name = {repository.name: repository for repository in repositories}
    assert set(by_name) == {"web", "api"}
    assert by_name["web"].id == "11"
    assert by_name["web"].owner == "acme"
    assert by_name["web"].private is False
    assert by_name["web"].default_branch == "develop"
    assert by_name["web"].html_url == "https://github.com/acme/web"
    # Nothing records when a repository joined an installation (spec 002 §3).
    assert by_name["web"].connected_at is None
    assert by_name["web"].last_run_at is None


async def test_a_missing_default_branch_falls_back_to_main(auth_clock: FixedClock) -> None:
    gateway, _ = gateway_for(auth_clock, routed())

    repositories = await gateway.list_repositories(42)

    api = next(item for item in repositories if item.name == "api")
    assert api.default_branch == "main"
    assert api.private is True


async def test_the_installation_token_is_used_as_bearer(auth_clock: FixedClock) -> None:
    gateway, transport = gateway_for(auth_clock, routed())

    await gateway.list_repositories(42)

    listing = [
        request
        for request in transport.requests  # type: ignore[attr-defined]
        if "installation/repositories" in str(request.url)
    ]
    assert listing[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert dict(listing[0].url.params)["per_page"] == str(PER_PAGE)


async def test_a_second_page_is_followed(auth_clock: FixedClock) -> None:
    def many(count: int, start: int) -> list[dict[str, object]]:
        return [
            {
                "id": start + index,
                "name": f"repo-{start + index}",
                "private": False,
                "default_branch": "main",
                "html_url": "https://github.com/acme/x",
                "owner": {"login": "acme"},
            }
            for index in range(count)
        ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json=github_fixture("installation_token"))
        page_number = int(dict(request.url.params).get("page", "1"))
        batch = many(PER_PAGE, 0) if page_number == 1 else many(5, 1000)
        return httpx.Response(200, json={"total_count": 105, "repositories": batch})

    gateway, transport = gateway_for(auth_clock, handler)

    repositories = await gateway.list_repositories(42)

    assert len(repositories) == 105
    pages = [
        dict(request.url.params).get("page")
        for request in transport.requests  # type: ignore[attr-defined]
        if "installation/repositories" in str(request.url)
    ]
    assert pages == ["1", "2"]


async def test_paging_stops_at_the_cap_even_if_total_count_lies(
    auth_clock: FixedClock,
) -> None:
    """Without a cap a wrong total_count would loop until the process died."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json=github_fixture("installation_token"))
        return httpx.Response(
            200,
            json={
                "total_count": 10_000,
                "repositories": [
                    {
                        "id": index,
                        "name": f"r{index}",
                        "private": False,
                        "html_url": "",
                        "owner": {"login": "acme"},
                    }
                    for index in range(PER_PAGE)
                ],
            },
        )

    gateway, transport = gateway_for(auth_clock, handler)

    repositories = await gateway.list_repositories(42)

    assert len(repositories) == MAX_PAGES * PER_PAGE
    listing = [
        request
        for request in transport.requests  # type: ignore[attr-defined]
        if "installation/repositories" in str(request.url)
    ]
    assert len(listing) == MAX_PAGES


@pytest.mark.parametrize("status", [403, 404])
async def test_uninstalled_or_suspended_is_installation_gone(
    auth_clock: FixedClock, status: int
) -> None:
    gateway, _ = gateway_for(auth_clock, routed(status))

    with pytest.raises(InstallationGoneError):
        await gateway.list_repositories(42)


async def test_another_failure_is_forge_unavailable(auth_clock: FixedClock) -> None:
    gateway, _ = gateway_for(auth_clock, routed(503))

    with pytest.raises(ForgeUnavailableError, match="HTTP 503"):
        await gateway.list_repositories(42)


async def test_a_malformed_entry_is_skipped_rather_than_guessed_at(
    auth_clock: FixedClock,
) -> None:
    gateway, _ = gateway_for(
        auth_clock,
        routed(
            body={
                "total_count": 2,
                "repositories": [
                    {"id": 1, "name": "ok", "owner": {"login": "acme"}, "html_url": ""},
                    {"id": 2, "name": "no-owner", "html_url": ""},
                ],
            }
        ),
    )

    repositories = await gateway.list_repositories(42)

    assert [item.name for item in repositories] == ["ok"]


async def test_invalid_json_is_forge_unavailable(auth_clock: FixedClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json=github_fixture("installation_token"))
        return httpx.Response(200, text="not json")

    gateway, _ = gateway_for(auth_clock, handler)

    with pytest.raises(ForgeUnavailableError, match="invalid JSON"):
        await gateway.list_repositories(42)
