"""End-to-end sign-in over TestClient, with a fake provider and a fixed clock."""

import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from api.deps import OAUTH_COOKIE, SESSION_COOKIE
from domain.errors import IdentityProviderError
from domain.ports import Profile
from tests.conftest import (
    TEST_ORIGIN,
    FakeIdentityProvider,
    FixedClock,
    make_organization,
    make_user,
)

CODE = "gho-test-code-aaaa"
CSRF_HEADERS = {"X-Requested-With": "fetch", "Origin": TEST_ORIGIN}


def start(client: TestClient, return_to: str | None = None) -> httpx.Response:
    params = {"return_to": return_to} if return_to is not None else {}
    response = client.get("/auth/github", params=params, follow_redirects=False)
    assert response.status_code == 302
    return response


def state_of(response: httpx.Response) -> str:
    return str(httpx.URL(response.headers["location"]).params["state"])


def callback(
    client: TestClient,
    *,
    state: str | None = None,
    code: str | None = CODE,
    error: str | None = None,
) -> httpx.Response:
    params: dict[str, str] = {}
    if state is not None:
        params["state"] = state
    if code is not None:
        params["code"] = code
    if error is not None:
        params["error"] = error
    return client.get("/auth/github/callback", params=params, follow_redirects=False)


def sign_in(client: TestClient, return_to: str | None = None) -> httpx.Response:
    return callback(client, state=state_of(start(client, return_to)))


def result_of(response: httpx.Response) -> str:
    return str(httpx.URL(response.headers["location"]).params["result"])


def session_cookie_header(response: httpx.Response) -> str | None:
    for raw in response.headers.get_list("set-cookie"):
        if raw.startswith(f"{SESSION_COOKIE}=") and not raw.startswith(f"{SESSION_COOKIE}=;"):
            return raw
    return None


# --- start --------------------------------------------------------------


def test_start_redirects_to_the_provider_and_binds_the_attempt(
    client: TestClient, identity: FakeIdentityProvider
) -> None:
    response = start(client, "/runs")

    assert response.headers["location"].startswith("https://github.example.test/")
    assert OAUTH_COOKIE in client.cookies
    assert response.headers["cache-control"] == "no-store"
    # PKCE: a challenge is derived and sent, the verifier is not.
    state, challenge = identity.authorize_calls[0]
    assert state and challenge
    assert "=" not in challenge


def test_an_unknown_provider_is_404(client: TestClient) -> None:
    assert client.get("/auth/gitlab", follow_redirects=False).status_code == 404


# --- state --------------------------------------------------------------


def test_a_missing_attempt_cookie_is_a_state_mismatch(client: TestClient) -> None:
    response = callback(client, state="anything")

    assert result_of(response) == "state_mismatch"
    assert session_cookie_header(response) is None


def test_a_mismatched_state_is_a_state_mismatch(client: TestClient) -> None:
    start(client)

    response = callback(client, state="not-the-state")

    assert result_of(response) == "state_mismatch"
    assert session_cookie_header(response) is None


def test_a_missing_state_is_a_state_mismatch(client: TestClient) -> None:
    start(client)

    response = callback(client, state=None)

    assert result_of(response) == "state_mismatch"


def test_a_stale_attempt_is_a_state_mismatch(client: TestClient, auth_clock: FixedClock) -> None:
    state = state_of(start(client))

    auth_clock.advance(600)

    assert result_of(callback(client, state=state)) == "state_mismatch"


def test_a_replayed_state_is_a_state_mismatch(client: TestClient) -> None:
    state = state_of(start(client))
    assert result_of(callback(client, state=state)) == "success"

    assert result_of(callback(client, state=state)) == "state_mismatch"


# --- the three failure results -----------------------------------------


def test_access_denied_is_reported_as_itself(client: TestClient) -> None:
    state = state_of(start(client))

    response = callback(client, state=state, code=None, error="access_denied")

    assert result_of(response) == "access_denied"
    assert session_cookie_header(response) is None


def test_an_exchange_failure_is_a_server_error(
    auth_clock: FixedClock, identity: FakeIdentityProvider
) -> None:
    identity.exchange_error = IdentityProviderError("github", "token exchange rejected: bad_code")
    client = _client_with(auth_clock, identity)

    response = callback(client, state=state_of(start(client)))

    assert result_of(response) == "server_error"
    assert session_cookie_header(response) is None


def test_a_profile_failure_is_a_server_error(
    auth_clock: FixedClock, identity: FakeIdentityProvider
) -> None:
    identity.profile_error = IdentityProviderError("github", "GET timed out")
    client = _client_with(auth_clock, identity)

    assert result_of(callback(client, state=state_of(start(client)))) == "server_error"


# --- success ------------------------------------------------------------


def test_success_redirects_with_the_sanitised_return_to(client: TestClient) -> None:
    response = sign_in(client, "/runs")

    location = response.headers["location"]
    assert location.startswith("/auth/callback?")
    assert httpx.URL(location).params["result"] == "success"
    assert httpx.URL(location).params["return_to"] == "/runs"
    assert "return_to=%2Fruns" in location


def test_an_unsafe_return_to_is_dropped(client: TestClient) -> None:
    response = sign_in(client, "//evil.example")

    assert result_of(response) == "success"
    assert "return_to" not in dict(httpx.URL(response.headers["location"]).params)


def test_the_session_cookie_has_the_agreed_attributes(client: TestClient) -> None:
    raw = session_cookie_header(sign_in(client))

    assert raw is not None
    assert "HttpOnly" in raw
    assert "Secure" in raw
    assert "SameSite=Strict" in raw or "SameSite=strict" in raw
    assert "Path=/" in raw
    assert "Max-Age=3600" in raw
    assert "Domain=" not in raw


def test_every_callback_outcome_sends_no_referrer(client: TestClient) -> None:
    assert callback(client, state="x").headers["referrer-policy"] == "no-referrer"
    assert sign_in(client).headers["referrer-policy"] == "no-referrer"


def test_the_attempt_cookie_is_cleared_on_the_callback(client: TestClient) -> None:
    sign_in(client)

    cleared = [
        raw
        for raw in sign_in(client).headers.get_list("set-cookie")
        if raw.startswith(f"{OAUTH_COOKIE}=")
    ]
    assert cleared and "Max-Age=0" in cleared[0]


def test_signing_in_twice_issues_a_different_session_id(client: TestClient) -> None:
    first = session_cookie_header(sign_in(client))
    second = session_cookie_header(sign_in(client))

    assert first is not None and second is not None
    assert first != second


# --- /me ----------------------------------------------------------------


def test_me_without_a_session_is_401_no_session(client: TestClient) -> None:
    response = client.get("/me")

    assert response.status_code == 401
    assert response.json() == {"error": "no_session"}


def test_me_returns_the_agreed_shape(
    auth_clock: FixedClock, identity: FakeIdentityProvider
) -> None:
    identity.profile = Profile(
        user=make_user(),
        organizations=(make_organization("zulu", "9"), make_organization("acme", "100")),
    )
    client = _client_with(auth_clock, identity)
    sign_in(client)

    body = client.get("/me").json()

    assert body["user"] == {
        "id": "1",
        "login": "octocat",
        "name": "Octo Cat",
        "avatar_url": "https://avatars.githubusercontent.com/u/1",
        "is_platform_admin": False,
    }
    assert [org["login"] for org in body["organizations"]] == ["zulu", "acme"]
    assert body["organizations"][0]["role"] in {"owner", "member"}
    # The first organisation by login.
    assert body["current_organization_id"] == "100"


def test_me_reports_a_null_current_organization_when_installed_nowhere(
    auth_clock: FixedClock, identity: FakeIdentityProvider
) -> None:
    identity.profile = Profile(user=make_user(), organizations=())
    client = _client_with(auth_clock, identity)
    sign_in(client)

    assert client.get("/me").json()["current_organization_id"] is None


def test_me_never_leaks_the_internal_installation_fields(client: TestClient) -> None:
    """Spec 002 Step 0: installation_id and account_type are internal."""
    sign_in(client)

    body = client.get("/me").json()

    assert set(body) == {"user", "organizations", "current_organization_id"}
    for organization in body["organizations"]:
        assert set(organization) == {"id", "login", "name", "avatar_url", "role"}
    assert "installation_id" not in client.get("/me").text
    assert "account_type" not in client.get("/me").text


def test_signing_in_again_leaves_exactly_one_session(client: TestClient) -> None:
    """Spec 002 Step 0: /github/setup re-runs sign-in, and the previous session
    must not survive it."""
    sign_in(client)
    first = client.cookies[SESSION_COOKIE]

    sign_in(client)
    second = client.cookies[SESSION_COOKIE]

    assert first != second
    assert len(client.sessions) == 1  # type: ignore[attr-defined]

    # The old cookie is dead, the new one works.
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, first)
    assert client.get("/me").status_code == 401
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, second)
    assert client.get("/me").status_code == 200


def test_me_is_no_store(client: TestClient) -> None:
    sign_in(client)

    assert client.get("/me").headers["cache-control"] == "no-store"


def test_me_is_401_once_the_idle_timeout_has_passed(
    client: TestClient, auth_clock: FixedClock
) -> None:
    sign_in(client)

    auth_clock.advance(3600)

    assert client.get("/me").status_code == 401


def test_me_extends_the_expiry_while_it_is_used(client: TestClient, auth_clock: FixedClock) -> None:
    sign_in(client)

    auth_clock.advance(1800)
    assert client.get("/me").status_code == 200

    # Without the slide this would now be past the original hour.
    auth_clock.advance(1800)
    assert client.get("/me").status_code == 200


def test_me_resends_the_cookie_so_the_browser_keeps_it(client: TestClient) -> None:
    sign_in(client)

    response = client.get("/me")

    assert session_cookie_header(response) is not None


# --- logout and CSRF ----------------------------------------------------


def test_logout_is_204_with_a_session(client: TestClient) -> None:
    sign_in(client)

    response = client.post("/auth/logout", headers=CSRF_HEADERS)

    assert response.status_code == 204


def test_logout_is_204_without_a_session(client: TestClient) -> None:
    assert client.post("/auth/logout", headers=CSRF_HEADERS).status_code == 204


def test_the_old_cookie_is_rejected_after_logout(client: TestClient) -> None:
    sign_in(client)
    old_value = client.cookies[SESSION_COOKIE]
    client.post("/auth/logout", headers=CSRF_HEADERS)

    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, old_value)

    assert client.get("/me").status_code == 401


def test_logout_without_the_requested_with_header_is_csrf(client: TestClient) -> None:
    response = client.post("/auth/logout", headers={"Origin": TEST_ORIGIN})

    assert response.status_code == 403
    assert response.json() == {"error": "csrf"}


def test_logout_from_a_foreign_origin_is_csrf(client: TestClient) -> None:
    response = client.post(
        "/auth/logout", headers={"X-Requested-With": "fetch", "Origin": "https://evil.example"}
    )

    assert response.status_code == 403
    assert response.json() == {"error": "csrf"}


def test_get_requests_need_no_csrf_headers(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


# --- the stub routes keep working --------------------------------------


def test_root_and_health_still_answer(client: TestClient) -> None:
    assert client.get("/").json() == {"message": "Welcome to DMC-268 Team 4 API"}
    assert client.get("/health").json() == {"status": "ok"}


# --- logging ------------------------------------------------------------


SECRETS = (CODE, "ghu_test_aaaaaaaaaaaaaaaaaaaa")


def test_no_log_record_carries_the_code_the_state_or_a_token(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        started = start(client, "/runs")
        state = state_of(started)
        # A rejection and a success, so both logging paths are exercised.
        callback(client, state="not-the-state")
        callback(client, state=state)
        client.get("/me")
        client.post("/auth/logout", headers=CSRF_HEADERS)

    ours = [record for record in caplog.records if not record.name.startswith("httpx")]
    assert ours, "expected the sign-in flow to log something"
    haystack = "\n".join(
        record.getMessage() + " " + " ".join(str(value) for value in record.__dict__.values())
        for record in ours
    )

    for secret in (*SECRETS, state):
        assert secret not in haystack


def _client_with(clock: FixedClock, identity: FakeIdentityProvider) -> TestClient:
    from tests.conftest import build_test_app

    return TestClient(build_test_app(clock, identity), base_url=TEST_ORIGIN)
