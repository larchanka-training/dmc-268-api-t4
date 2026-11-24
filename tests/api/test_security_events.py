"""security_events_total (docs/observability.md § Metrics): one kind per refusal path,
and nothing for the ordinary cases next to them (signed out, a route without an id)."""

from fastapi.testclient import TestClient

from adapters.observability import metrics
from api.deps import SESSION_COOKIE
from tests.api.test_auth import CSRF_HEADERS, callback, sign_in, start
from tests.conftest import TEST_ORIGIN, FixedClock


def events(kind: str) -> float:
    return metrics.API_REGISTRY.get_sample_value("security_events_total", {"kind": kind}) or 0.0


def test_a_forged_oauth_state_is_counted(client: TestClient) -> None:
    before = events("oauth_state_mismatch")
    start(client)

    callback(client, state="not-the-state")

    assert events("oauth_state_mismatch") == before + 1


def test_both_csrf_refusals_are_counted(client: TestClient) -> None:
    before = events("csrf_rejected")

    client.post("/auth/logout", headers={"Origin": TEST_ORIGIN})
    client.post("/auth/logout", headers={"X-Requested-With": "fetch", "Origin": "https://evil"})

    assert events("csrf_rejected") == before + 2


def test_an_unknown_or_expired_session_cookie_is_counted(
    client: TestClient, auth_clock: FixedClock
) -> None:
    before = events("session_rejected")
    client.cookies.set(SESSION_COOKIE, "a-session-we-never-issued")
    assert client.get("/me").status_code == 401

    client.cookies.clear()
    sign_in(client)
    auth_clock.advance(10 * 24 * 3600)
    assert client.get("/me").status_code == 401

    assert events("session_rejected") == before + 2


def test_no_cookie_at_all_is_not_a_security_event(client: TestClient) -> None:
    before = events("session_rejected")

    assert client.get("/me").status_code == 401

    assert events("session_rejected") == before


def test_an_id_that_is_not_there_is_counted(client: TestClient) -> None:
    sign_in(client)
    before = events("id_not_found")

    response = client.get("/jobs/0123456789abcdef0123456789abcdef", headers=CSRF_HEADERS)

    assert response.status_code == 404
    assert events("id_not_found") == before + 1


def test_a_404_without_an_id_in_the_path_is_not(client: TestClient) -> None:
    before = events("id_not_found")

    # The relay's route has no id; without its secret it answers 404.
    assert client.post("/internal/alerts/sentry").status_code == 404

    assert events("id_not_found") == before
