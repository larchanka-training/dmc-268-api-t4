"""api/access_log.py: one line per request, the route template and never the raw
path or query string, and a request_id on every line logged while handling it.

Requests go through FastAPI's TestClient against the test app; nothing leaves the
process."""

import io
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.access_log import REQUEST_ID_HEADER, UNMATCHED_ROUTE
from tests.conftest import (
    TEST_ORIGIN,
    FakeIdentityProvider,
    FixedClock,
    all_log_text,
    build_test_app,
    json_lines,
    log_events,
)


@pytest.fixture
def app_client(auth_clock: FixedClock) -> Any:
    with TestClient(build_test_app(auth_clock, FakeIdentityProvider()), base_url=TEST_ORIGIN) as c:
        yield c


def test_a_request_logs_the_route_template_never_the_query_string(
    app_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="api.access"):
        app_client.get(
            "/auth/github/callback?code=CODE-MARKER&state=STATE-MARKER", follow_redirects=False
        )

    [record] = log_events(caplog, "http.request")
    assert getattr(record, "http.route") == "/auth/{provider}/callback"
    assert getattr(record, "http.method") == "GET"
    assert getattr(record, "http.status") in (302, 303, 307)
    assert isinstance(record.duration_ms, int)
    text = all_log_text(caplog)
    assert "CODE-MARKER" not in text
    assert "STATE-MARKER" not in text


def test_an_unknown_path_is_logged_as_unmatched(
    app_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="api.access"):
        app_client.get("/no/such/PATH-MARKER")

    [record] = log_events(caplog, "http.request")
    assert getattr(record, "http.route") == UNMATCHED_ROUTE
    assert getattr(record, "http.status") == 404
    assert "PATH-MARKER" not in all_log_text(caplog)


def test_a_csrf_rejection_is_logged_too(
    app_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="api.access"):
        response = app_client.post("/auth/logout")

    assert response.status_code == 403
    [record] = log_events(caplog, "http.request")
    assert getattr(record, "http.status") == 403


def test_the_healthcheck_is_logged_at_debug(
    app_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="api.access"):
        app_client.get("/health")

    [record] = log_events(caplog, "http.request")
    assert record.levelno == logging.DEBUG


def test_the_response_echoes_a_new_request_id(app_client: TestClient) -> None:
    first = app_client.get("/health").headers[REQUEST_ID_HEADER]
    second = app_client.get("/health").headers[REQUEST_ID_HEADER]

    assert len(first) == 32
    assert first != second


def test_a_plain_caller_request_id_is_kept_and_anything_else_replaced(
    app_client: TestClient,
) -> None:
    kept = app_client.get("/health", headers={REQUEST_ID_HEADER: "abc-123"})
    replaced = app_client.get("/health", headers={REQUEST_ID_HEADER: "has spaces " + "x" * 80})

    assert kept.headers[REQUEST_ID_HEADER] == "abc-123"
    assert replaced.headers[REQUEST_ID_HEADER] != "has spaces " + "x" * 80


def test_every_line_of_a_request_carries_its_request_id(
    app_client: TestClient, json_logs: io.StringIO
) -> None:
    response = app_client.get("/github/setup", follow_redirects=False)
    request_id = response.headers[REQUEST_ID_HEADER]

    lines = [line for line in json_lines(json_logs) if line.get("request_id") == request_id]
    assert {line["event"] for line in lines} >= {"github.setup.redirect", "http.request"}
