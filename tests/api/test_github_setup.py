"""The Setup URL: GitHub's redirect back after an install or a repository change.

GitHub warns that installation_id can be spoofed, so this route reads none of
GitHub's parameters and re-runs sign-in instead.
"""

import logging
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from domain.ports import Profile
from tests.conftest import (
    TEST_ORIGIN,
    FakeIdentityProvider,
    FakeInstallationGateway,
    FixedClock,
    make_organization,
    make_repository,
    make_user,
)

ORG = make_organization("acme", "100", installation_id=42)
EXPECTED_RETURN_TO = "/repositories?connected=1"


def build(
    clock: FixedClock, organizations: tuple[object, ...] = (ORG,)
) -> tuple[TestClient, FakeInstallationGateway]:
    from datetime import timedelta

    from adapters.memory.repositories import InMemoryRepositoriesCache
    from domain.repositories import RepositoriesService
    from tests.conftest import build_test_app

    identity = FakeIdentityProvider(
        Profile(user=make_user(), organizations=organizations)  # type: ignore[arg-type]
    )
    gateway = FakeInstallationGateway((make_repository(),))
    service = RepositoriesService(
        gateway=gateway,
        cache=InMemoryRepositoriesCache(clock),
        clock=clock,
        cache_ttl=timedelta(seconds=60),
    )
    app = build_test_app(clock, identity, repositories=service)
    return TestClient(app, base_url=TEST_ORIGIN), gateway


def sign_in(client: TestClient) -> None:
    start = client.get("/auth/github", follow_redirects=False)
    state = str(httpx.URL(start.headers["location"]).params["state"])
    client.get(
        "/auth/github/callback",
        params={"state": state, "code": "c"},
        follow_redirects=False,
    )


def test_it_redirects_to_sign_in_with_the_agreed_return_to(auth_clock: FixedClock) -> None:
    client, _ = build(auth_clock)

    response = client.get("/github/setup", follow_redirects=False)

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("/api/auth/github?")
    assert unquote(str(httpx.URL(location).params["return_to"])) == EXPECTED_RETURN_TO


def test_it_works_without_a_session(auth_clock: FixedClock) -> None:
    client, _ = build(auth_clock)

    response = client.get("/github/setup", follow_redirects=False)

    assert response.status_code == 302


def test_it_sends_no_referrer_and_is_no_store(auth_clock: FixedClock) -> None:
    client, _ = build(auth_clock)

    response = client.get("/github/setup", follow_redirects=False)

    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"


def test_it_drops_the_cache_so_the_next_list_refetches(auth_clock: FixedClock) -> None:
    client, gateway = build(auth_clock)
    sign_in(client)
    client.get("/repositories")
    assert gateway.calls == [42]

    client.get("/github/setup", follow_redirects=False)
    client.get("/repositories")

    assert gateway.calls == [42, 42]


def test_a_spoofed_installation_id_changes_nothing(auth_clock: FixedClock) -> None:
    """GitHub: "you should not rely on the validity of the installation_id"."""
    client, gateway = build(auth_clock)
    sign_in(client)
    client.get("/repositories")

    honest = client.get("/github/setup", follow_redirects=False)
    spoofed = client.get(
        "/github/setup",
        params={"installation_id": "999999", "setup_action": "install"},
        follow_redirects=False,
    )

    # Same response, and the only installation ever asked for is the session's.
    assert spoofed.status_code == honest.status_code
    assert spoofed.headers["location"] == honest.headers["location"]
    client.get("/repositories")
    assert set(gateway.calls) == {42}


def test_the_parameters_are_never_logged(
    auth_clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    client, _ = build(auth_clock)

    with caplog.at_level(logging.DEBUG):
        client.get(
            "/github/setup",
            params={"installation_id": "999999", "setup_action": "install"},
            follow_redirects=False,
        )

    ours = [record for record in caplog.records if not record.name.startswith("httpx")]
    assert ours, "the route should log that it redirected"
    haystack = "\n".join(
        record.getMessage() + " " + " ".join(str(value) for value in record.__dict__.values())
        for record in ours
    )
    assert "999999" not in haystack
    assert "setup_action" not in haystack


def test_signing_in_after_setup_lands_on_the_repositories_page(
    auth_clock: FixedClock,
) -> None:
    """End to end with fakes: setup, sign-in, callback, then /me shows the org."""
    client, _ = build(auth_clock)

    setup = client.get("/github/setup", follow_redirects=False)
    return_to = str(httpx.URL(setup.headers["location"]).params["return_to"])

    start = client.get("/auth/github", params={"return_to": return_to}, follow_redirects=False)
    state = str(httpx.URL(start.headers["location"]).params["state"])
    callback = client.get(
        "/auth/github/callback",
        params={"state": state, "code": "c"},
        follow_redirects=False,
    )

    location = httpx.URL(callback.headers["location"])
    assert str(location.params["result"]) == "success"
    assert unquote(str(location.params["return_to"])) == EXPECTED_RETURN_TO
    assert [org["login"] for org in client.get("/me").json()["organizations"]] == ["acme"]
