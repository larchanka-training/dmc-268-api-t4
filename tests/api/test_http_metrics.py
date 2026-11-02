"""HTTP metrics from api/access_log.py: the route template as the label, never the raw
path; the SSE stream counted but not timed; and no metrics route on the API's main
port, which Caddy proxies (docs/observability.md § Metrics).

Requests go through FastAPI's TestClient against the test app; the registries are
read in process.
"""

from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from adapters.observability import metrics
from api.access_log import UNMATCHED_ROUTE
from tests.conftest import TEST_ORIGIN, FakeIdentityProvider, FixedClock, build_test_app

JOB_ID = "0192f1c47a2b7c3d8e4f5a6b7c8d9e0f"


@pytest.fixture
def app_client(auth_clock: FixedClock) -> Any:
    with TestClient(build_test_app(auth_clock, FakeIdentityProvider()), base_url=TEST_ORIGIN) as c:
        yield c


def requests_count(route: str, method: str, status_class: str) -> float:
    labels = {"route": route, "method": method, "status_class": status_class}
    return metrics.API_REGISTRY.get_sample_value("http_requests_total", labels) or 0.0


def timed_count(route: str, method: str) -> float | None:
    labels = {"route": route, "method": method}
    return metrics.API_REGISTRY.get_sample_value("http_request_duration_seconds_count", labels)


def test_the_main_port_serves_no_metrics(app_client: TestClient) -> None:
    assert app_client.get("/metrics").status_code == 404


def test_a_request_is_counted_under_its_route_template(app_client: TestClient) -> None:
    before = requests_count("/jobs/{job_id}", "GET", "4xx")
    timed = timed_count("/jobs/{job_id}", "GET") or 0.0

    assert app_client.get(f"/jobs/{JOB_ID}").status_code == 401

    assert requests_count("/jobs/{job_id}", "GET", "4xx") == before + 1
    assert timed_count("/jobs/{job_id}", "GET") == timed + 1
    labels = [
        value
        for family in metrics.API_REGISTRY.collect()
        for sample in family.samples
        for value in sample.labels.values()
    ]
    assert not [value for value in labels if JOB_ID in value]


def test_the_event_stream_is_counted_but_not_timed(app_client: TestClient) -> None:
    before = requests_count("/jobs/{job_id}/events", "GET", "4xx")

    assert app_client.get(f"/jobs/{JOB_ID}/events").status_code == 401

    assert requests_count("/jobs/{job_id}/events", "GET", "4xx") == before + 1
    assert timed_count("/jobs/{job_id}/events", "GET") is None


def test_an_unknown_path_is_counted_as_unmatched(app_client: TestClient) -> None:
    before = requests_count(UNMATCHED_ROUTE, "GET", "4xx")

    app_client.get("/no/such/PATH-MARKER")

    assert requests_count(UNMATCHED_ROUTE, "GET", "4xx") == before + 1


def test_every_route_of_the_app_has_its_series_from_the_start(auth_clock: FixedClock) -> None:
    app = build_test_app(auth_clock, FakeIdentityProvider())
    routes = [route for route in app.routes if isinstance(route, APIRoute)]

    assert routes
    for route in routes:
        for method in route.methods:
            for status_class in metrics.STATUS_CLASSES:
                labels = {"route": route.path, "method": method, "status_class": status_class}
                assert (
                    metrics.API_REGISTRY.get_sample_value("http_requests_total", labels) is not None
                ), labels
