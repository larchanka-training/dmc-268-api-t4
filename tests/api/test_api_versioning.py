"""Issue #43: every route lives under /v1, and the root copies keep working
while GitHub and the frontend move over (docs/api-versioning.md)."""

import re
from typing import Any

import httpx
import pytest

from tests.api.test_webhook_github import opened_body, sign
from tests.conftest import TEST_ORIGIN, FakeIdentityProvider, FixedClock, build_test_app

#: Routes outside the versioned API: the welcome page and the probes.
UNVERSIONED = {"/", "/health"}


def test_the_schema_lists_only_versioned_routes(client: Any) -> None:
    paths = set(client.get("/openapi.json").json()["paths"])

    assert paths - UNVERSIONED
    assert all(path.startswith("/v1/") for path in paths - UNVERSIONED)


def test_every_versioned_get_route_is_also_served_from_the_root(client: Any) -> None:
    schema_paths = client.get("/openapi.json").json()["paths"]
    get_paths = [
        path
        for path, methods in schema_paths.items()
        if path not in UNVERSIONED and "get" in methods
    ]

    assert get_paths
    for path in get_paths:
        legacy = re.sub(r"\{[^}]+\}", "x", path.removeprefix("/v1"))
        # Routing's own 404 is FastAPI's {"detail": ...}; a route that ran answers
        # with the agreed {"error": ...} shape, a redirect, or a stream.
        response = client.get(legacy, follow_redirects=False)
        assert response.headers.get("content-type") != "application/json" or (
            response.json() != {"detail": "Not Found"}
        ), legacy


@pytest.mark.parametrize("path", ["/v1/me", "/me"])
def test_me_answers_on_both_paths(client: Any, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 401
    assert response.json() == {"error": "no_session"}


def test_health_stays_unversioned(client: Any) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/v1/health").status_code == 404


@pytest.mark.parametrize("path", ["/v1/webhooks/github", "/webhooks/github"])
async def test_a_webhook_delivery_skips_csrf_on_both_paths(
    auth_clock: FixedClock, path: str
) -> None:
    app = build_test_app(auth_clock, FakeIdentityProvider())
    body = opened_body(action="edited")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=TEST_ORIGIN
    ) as http:
        response = await http.post(
            path,
            content=body,
            headers={
                "X-Hub-Signature-256": sign(body),
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "delivery-43",
            },
        )

    # 204: verified and acknowledged, not a 403 from the CSRF check.
    assert response.status_code == 204
