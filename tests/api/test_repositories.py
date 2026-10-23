"""The repository endpoints over TestClient, with fake ports."""

import httpx
from fastapi.testclient import TestClient

from domain.errors import ForgeUnavailableError, InstallationGoneError
from domain.ports import Profile
from domain.tenancy import AccountType
from tests.conftest import (
    APP_SLUG,
    TEST_ORIGIN,
    FakeIdentityProvider,
    FakeInstallationGateway,
    FixedClock,
    make_organization,
    make_repository,
    make_user,
)

ORG = make_organization("acme", "100", installation_id=42)


def sign_in(client: TestClient) -> None:
    start = client.get("/auth/github", follow_redirects=False)
    state = str(httpx.URL(start.headers["location"]).params["state"])
    client.get(
        "/auth/github/callback", params={"state": state, "code": "c"}, follow_redirects=False
    )


def client_for(
    clock: FixedClock,
    *,
    organizations: tuple[object, ...] = (ORG,),
    repositories: tuple[object, ...] = (),
    error: Exception | None = None,
) -> tuple[TestClient, FakeInstallationGateway]:
    from datetime import timedelta

    from adapters.memory.repositories import InMemoryRepositoriesCache
    from domain.repositories import RepositoriesService
    from tests.conftest import build_test_app

    identity = FakeIdentityProvider(
        Profile(user=make_user(), organizations=organizations)  # type: ignore[arg-type]
    )
    gateway = FakeInstallationGateway(repositories, error=error)  # type: ignore[arg-type]
    service = RepositoriesService(
        gateway=gateway,
        cache=InMemoryRepositoriesCache(clock),
        clock=clock,
        cache_ttl=timedelta(seconds=60),
    )
    app = build_test_app(clock, identity, repositories=service)
    test_client = TestClient(app, base_url=TEST_ORIGIN)
    sign_in(test_client)
    return test_client, gateway


def many(count: int) -> tuple[object, ...]:
    return tuple(make_repository("acme", f"repo-{index:02d}") for index in range(count))


# --- the list -----------------------------------------------------------


def test_the_list_is_sorted_and_paged_like_the_console(auth_clock: FixedClock) -> None:
    client, _ = client_for(
        auth_clock,
        repositories=(make_repository("acme", "web"), make_repository("Acme", "API")),
    )

    body = client.get("/repositories").json()

    assert [item["name"] for item in body["items"]] == ["API", "web"]
    assert body["next_cursor"] is None
    assert body["total_count"] == 2


def test_a_page_holds_ten_and_the_cursor_is_the_offset(auth_clock: FixedClock) -> None:
    client, _ = client_for(auth_clock, repositories=many(25))

    first = client.get("/repositories").json()
    assert len(first["items"]) == 10
    assert first["next_cursor"] == "10"
    assert first["total_count"] == 25

    second = client.get("/repositories", params={"cursor": "10"}).json()
    assert second["items"][0]["name"] == "repo-10"
    assert second["total_count"] == 25


def test_a_repository_has_the_agreed_shape(auth_clock: FixedClock) -> None:
    client, _ = client_for(auth_clock, repositories=(make_repository("acme", "payments"),))

    item = client.get("/repositories").json()["items"][0]

    assert set(item) == {
        "id",
        "owner",
        "name",
        "private",
        "default_branch",
        "html_url",
        "connected_at",
        "last_run_at",
    }
    assert item["owner"] == "acme"
    assert item["private"] is True
    # Both null for now: spec 002 §3, and review jobs do not exist yet.
    assert item["connected_at"] is None
    assert item["last_run_at"] is None


def test_no_organisation_is_an_empty_page_and_github_is_untouched(
    auth_clock: FixedClock,
) -> None:
    client, gateway = client_for(auth_clock, organizations=(), repositories=many(3))

    body = client.get("/repositories").json()

    assert body == {"items": [], "next_cursor": None, "total_count": 0}
    assert gateway.calls == []


def test_a_second_request_within_the_ttl_does_not_call_github(
    auth_clock: FixedClock,
) -> None:
    client, gateway = client_for(auth_clock, repositories=many(3))

    client.get("/repositories")
    client.get("/repositories")

    assert gateway.calls == [42]


def test_an_installation_that_is_gone_is_an_empty_page(auth_clock: FixedClock) -> None:
    client, _ = client_for(auth_clock, error=InstallationGoneError(42, "HTTP 404"))

    response = client.get("/repositories")

    assert response.status_code == 200
    assert response.json()["total_count"] == 0


def test_a_forge_failure_is_502(auth_clock: FixedClock) -> None:
    client, _ = client_for(auth_clock, error=ForgeUnavailableError("github", "HTTP 503"))

    response = client.get("/repositories")

    assert response.status_code == 502
    assert response.json() == {"error": "forge_unavailable"}


def test_a_bad_cursor_is_400(auth_clock: FixedClock) -> None:
    client, _ = client_for(auth_clock, repositories=many(3))

    response = client.get("/repositories", params={"cursor": "-1"})

    assert response.status_code == 400
    assert response.json() == {"error": "bad_cursor"}


def test_the_bad_cursor_is_not_echoed_back(auth_clock: FixedClock) -> None:
    client, _ = client_for(auth_clock, repositories=many(3))

    response = client.get("/repositories", params={"cursor": "<script>"})

    assert response.status_code == 400
    assert "script" not in response.text


def test_the_list_needs_a_session(client: TestClient) -> None:
    response = client.get("/repositories")

    assert response.status_code == 401
    assert response.json() == {"error": "no_session"}


def test_the_list_is_no_store(auth_clock: FixedClock) -> None:
    client, _ = client_for(auth_clock, repositories=many(1))

    assert client.get("/repositories").headers["cache-control"] == "no-store"


# --- connect URL --------------------------------------------------------


def test_connect_url_points_at_an_organisations_installation(auth_clock: FixedClock) -> None:
    client, _ = client_for(auth_clock)

    body = client.get("/repositories/connect-url").json()

    assert body == {"url": "https://github.com/organizations/acme/settings/installations/42"}


def test_connect_url_points_at_a_personal_installation(auth_clock: FixedClock) -> None:
    personal = make_organization("octocat", "1", installation_id=7, account_type=AccountType.USER)
    client, _ = client_for(auth_clock, organizations=(personal,))

    body = client.get("/repositories/connect-url").json()

    assert body == {"url": "https://github.com/settings/installations/7"}


def test_connect_url_points_at_the_install_page_without_an_organisation(
    auth_clock: FixedClock,
) -> None:
    client, _ = client_for(auth_clock, organizations=())

    body = client.get("/repositories/connect-url").json()

    assert body == {"url": f"https://github.com/apps/{APP_SLUG}/installations/new"}


def test_connect_url_is_given_to_members_too(auth_clock: FixedClock) -> None:
    from domain.tenancy import OrganizationRole

    member_org = make_organization("acme", "100", OrganizationRole.MEMBER, installation_id=42)
    client, _ = client_for(auth_clock, organizations=(member_org,))

    assert client.get("/repositories/connect-url").status_code == 200


def test_connect_url_needs_a_session(client: TestClient) -> None:
    assert client.get("/repositories/connect-url").status_code == 401


# --- disconnect URL ----------------------------------------------------


def test_disconnect_url_points_at_the_installation_settings(auth_clock: FixedClock) -> None:
    repository = make_repository("acme", "payments")
    client, _ = client_for(auth_clock, repositories=(repository,))

    body = client.get(f"/repositories/{repository.id}/disconnect-url").json()

    assert body == {"url": "https://github.com/organizations/acme/settings/installations/42"}


def test_disconnect_url_is_the_same_page_as_connect_url(auth_clock: FixedClock) -> None:
    """GitHub has no per-repository removal URL; the 404 is what this endpoint adds."""
    repository = make_repository("acme", "payments")
    client, _ = client_for(auth_clock, repositories=(repository,))

    disconnect = client.get(f"/repositories/{repository.id}/disconnect-url").json()
    connect = client.get("/repositories/connect-url").json()

    assert disconnect == connect


def test_disconnect_url_for_a_personal_installation(auth_clock: FixedClock) -> None:
    personal = make_organization("octocat", "1", installation_id=7, account_type=AccountType.USER)
    repository = make_repository("octocat", "dotfiles")
    client, _ = client_for(auth_clock, organizations=(personal,), repositories=(repository,))

    body = client.get(f"/repositories/{repository.id}/disconnect-url").json()

    assert body == {"url": "https://github.com/settings/installations/7"}


def test_an_unconnected_repository_is_404(auth_clock: FixedClock) -> None:
    client, _ = client_for(auth_clock, repositories=(make_repository(),))

    response = client.get("/repositories/999/disconnect-url")

    assert response.status_code == 404
    assert response.json() == {"error": "not_found"}


def test_disconnect_url_is_404_without_an_organisation(auth_clock: FixedClock) -> None:
    client, gateway = client_for(auth_clock, organizations=(), repositories=many(3))

    response = client.get("/repositories/acme-repo-00/disconnect-url")

    assert response.status_code == 404
    assert gateway.calls == []


def test_disconnect_url_is_404_when_the_installation_is_gone(
    auth_clock: FixedClock,
) -> None:
    client, _ = client_for(auth_clock, error=InstallationGoneError(42, "HTTP 404"))

    response = client.get("/repositories/anything/disconnect-url")

    assert response.status_code == 404
    assert response.json() == {"error": "not_found"}


def test_disconnect_url_finds_a_repository_beyond_the_first_page(
    auth_clock: FixedClock,
) -> None:
    client, _ = client_for(auth_clock, repositories=many(25))

    assert client.get("/repositories/acme-repo-24/disconnect-url").status_code == 200


def test_disconnect_url_reuses_the_cached_list(auth_clock: FixedClock) -> None:
    repository = make_repository("acme", "payments")
    client, gateway = client_for(auth_clock, repositories=(repository,))

    client.get("/repositories")
    client.get(f"/repositories/{repository.id}/disconnect-url")

    assert gateway.calls == [42]


def test_disconnect_url_needs_a_session(client: TestClient) -> None:
    response = client.get("/repositories/1/disconnect-url")

    assert response.status_code == 401
    assert response.json() == {"error": "no_session"}


def test_disconnect_url_is_no_store(auth_clock: FixedClock) -> None:
    repository = make_repository("acme", "payments")
    client, _ = client_for(auth_clock, repositories=(repository,))

    response = client.get(f"/repositories/{repository.id}/disconnect-url")

    assert response.headers["cache-control"] == "no-store"


def test_a_repository_id_that_looks_like_a_path_does_not_match_connect_url(
    auth_clock: FixedClock,
) -> None:
    """Route shapes must not collide: /repositories/connect-url is its own route."""
    client, _ = client_for(auth_clock, repositories=(make_repository(),))

    assert client.get("/repositories/connect-url").status_code == 200
    assert client.get("/repositories/connect-url/disconnect-url").status_code == 404
