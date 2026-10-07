"""The VCS client against recorded responses. No test opens a connection."""

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from adapters.github.app_auth import GitHubAppAuth
from adapters.github.client import GitHubVCSClient
from adapters.github.config import GitHubAppSettings
from domain.errors import ForgeUnavailableError
from tests.conftest import FixedClock, RecordingTransport, github_fixture

KEY_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "github" / "app_key.pem"
TOKEN = "ghs_test_aaaaaaaaaaaaaaaaaaaaaaaa"

REPO = "acme/web"
PR_NUMBER = 7
HEAD_SHA = "a1b2c3d4e5a1b2c3d4e5a1b2c3d4e5a1b2c3d4e5"
BASE_SHA = "0f1e2d3c4b0f1e2d3c4b0f1e2d3c4b0f1e2d3c4b"

PULL_DETAIL: dict[str, Any] = {
    "id": 1234567,
    "number": PR_NUMBER,
    "title": "Add payments webhook",
    "body": "Handles provider retries.",
    "user": {"login": "octocat", "id": 583231},
    "author_association": "CONTRIBUTOR",
    "head": {"sha": HEAD_SHA, "ref": "octocat/add-payments-webhook"},
    "base": {"sha": BASE_SHA, "ref": "main"},
}

DIFF_TEXT = """diff --git a/app/payments.py b/app/payments.py
--- a/app/payments.py
+++ b/app/payments.py
@@ -1,3 +1,4 @@
+import stripe
 def charge(amount: int) -> None:
-    pass
+    stripe.Charge(amount)
"""


def commit(index: int, login: str | None = "octocat") -> dict[str, Any]:
    return {
        "sha": f"{index:040x}",
        "commit": {"message": f"commit {index}"},
        "author": ({"login": login} if login is not None else None),
    }


def settings() -> GitHubAppSettings:
    return GitHubAppSettings(
        app_id="123456",
        private_key=KEY_PATH.read_text(encoding="utf-8"),
        app_slug="review-agent",
        webhook_secret="whsec_test_secret",
        api_base_url="https://api.github.test",
    )


def client_for(clock: FixedClock, handler: Any) -> tuple[GitHubVCSClient, RecordingTransport]:
    transport = RecordingTransport(handler)
    config = settings()
    auth = GitHubAppAuth(config, clock, transport=transport)
    return GitHubVCSClient(config, auth, transport=transport), transport


def serving(
    detail_status: int = 200,
    *,
    detail: dict[str, Any] | None = None,
    commit_pages: list[list[dict[str, Any]]] | None = None,
    diff_status: int = 200,
    diff_text: str = DIFF_TEXT,
) -> Any:
    """One handler serving the token, pull, commits and diff endpoints."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/access_tokens"):
            return httpx.Response(200, json=github_fixture("installation_token"))
        if path.endswith(f"/repos/{REPO}/pulls/{PR_NUMBER}"):
            if detail_status != 200:
                return httpx.Response(detail_status, json={"message": "secret-body-marker"})
            return httpx.Response(200, json=detail if detail is not None else PULL_DETAIL)
        if path.endswith(f"/repos/{REPO}/pulls/{PR_NUMBER}/commits"):
            page_number = int(dict(request.url.params).get("page", "1"))
            pages = commit_pages if commit_pages is not None else [[commit(0), commit(1)]]
            batch = pages[page_number - 1]
            headers = (
                {
                    "Link": f"<{settings().api_base_url}/repos/{REPO}/pulls/{PR_NUMBER}"
                    f'/commits?per_page=100&page={page_number + 1}>; rel="next"'
                }
                if page_number < len(pages)
                else {}
            )
            return httpx.Response(200, json=batch, headers=headers)
        if path.endswith(f"/repos/{REPO}/pulls/{PR_NUMBER}.diff"):
            if diff_status != 200:
                return httpx.Response(diff_status, json={"message": "secret-body-marker"})
            return httpx.Response(200, text=diff_text)
        raise AssertionError(f"unexpected request for {request.url}")

    return handler


# --- the happy path ------------------------------------------------------


async def test_every_field_is_mapped_exactly(auth_clock: FixedClock) -> None:
    client, _ = client_for(
        auth_clock,
        serving(commit_pages=[[commit(0, "octocat"), commit(1, None)]]),
    )

    context = await client.fetch_pull_request(42, REPO, PR_NUMBER)

    assert context.installation_id == 42
    assert context.repo_full_name == REPO
    assert context.pr_number == PR_NUMBER
    assert context.title == "Add payments webhook"
    assert context.description == "Handles provider retries."
    assert context.head_sha == HEAD_SHA
    assert context.base_sha == BASE_SHA
    assert context.head_ref == "octocat/add-payments-webhook"
    assert context.base_ref == "main"
    assert context.author_login == "octocat"
    assert context.author_external_id == 583231
    assert context.author_association == "CONTRIBUTOR"
    assert [item.message for item in context.commits] == ["commit 0", "commit 1"]
    assert [item.author_login for item in context.commits] == ["octocat", None]
    assert context.diff_text == DIFF_TEXT


async def test_a_null_body_becomes_an_empty_description(auth_clock: FixedClock) -> None:
    detail = {**PULL_DETAIL, "body": None, "user": None, "author_association": None}
    client, _ = client_for(auth_clock, serving(detail=detail))

    context = await client.fetch_pull_request(42, REPO, PR_NUMBER)

    assert context.description == ""
    assert context.author_login is None
    assert context.author_external_id is None
    assert context.author_association is None


async def test_the_installation_token_flows_through_every_request(
    auth_clock: FixedClock,
) -> None:
    client, transport = client_for(auth_clock, serving())

    await client.fetch_pull_request(42, REPO, PR_NUMBER)

    paths = [request.url.path for request in transport.requests]
    assert paths[0].endswith("/app/installations/42/access_tokens")
    for request in transport.requests[1:]:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
    diff_request = next(
        request for request in transport.requests if request.url.path.endswith(".diff")
    )
    assert diff_request.headers["accept"] == "application/vnd.github.diff"


async def test_a_redirect_is_followed_to_the_final_response(auth_clock: FixedClock) -> None:
    inner = serving()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/repos/{REPO}/pulls/{PR_NUMBER}":
            return httpx.Response(
                302, headers={"Location": f"/repos/{REPO}/pulls/{PR_NUMBER}/moved"}
            )
        if request.url.path == f"/repos/{REPO}/pulls/{PR_NUMBER}/moved":
            return httpx.Response(200, json=PULL_DETAIL)
        return inner(request)

    client, _ = client_for(auth_clock, handler)

    context = await client.fetch_pull_request(42, REPO, PR_NUMBER)

    assert context.title == "Add payments webhook"


# --- commit pagination ---------------------------------------------------


async def test_commit_pages_follow_the_link_header(auth_clock: FixedClock) -> None:
    pages = [[commit(index) for index in range(100)], [commit(100), commit(101), commit(102)]]
    client, transport = client_for(auth_clock, serving(commit_pages=pages))

    context = await client.fetch_pull_request(42, REPO, PR_NUMBER)

    assert len(context.commits) == 103
    listing = [
        dict(request.url.params).get("page")
        for request in transport.requests
        if request.url.path.endswith("/commits")
    ]
    assert listing == [None, "2"]


async def test_commit_paging_stops_at_five_pages(auth_clock: FixedClock) -> None:
    pages = [[commit(page * 100 + index) for index in range(100)] for page in range(10)]
    client, transport = client_for(auth_clock, serving(commit_pages=pages))

    context = await client.fetch_pull_request(42, REPO, PR_NUMBER)

    assert len(context.commits) == 500
    listing = [request for request in transport.requests if request.url.path.endswith("/commits")]
    assert len(listing) == 5  # the sixth page is never requested


async def test_relative_next_links_are_resolved_against_the_response_url(
    auth_clock: FixedClock,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/access_tokens"):
            return httpx.Response(200, json=github_fixture("installation_token"))
        if path.endswith(f"/repos/{REPO}/pulls/{PR_NUMBER}"):
            return httpx.Response(200, json=PULL_DETAIL)
        if path.endswith(f"/repos/{REPO}/pulls/{PR_NUMBER}/commits"):
            page_number = int(dict(request.url.params).get("page", "1"))
            headers = {"Link": f'<?page={page_number + 1}>; rel="next"'} if page_number == 1 else {}
            return httpx.Response(200, json=[commit(0)], headers=headers)
        if path.endswith(".diff"):
            return httpx.Response(200, text=DIFF_TEXT)
        raise AssertionError(f"unexpected request for {request.url}")

    client, transport = client_for(auth_clock, handler)

    context = await client.fetch_pull_request(42, REPO, PR_NUMBER)

    assert len(context.commits) == 2
    listing = [
        dict(request.url.params).get("page")
        for request in transport.requests
        if request.url.path.endswith("/commits")
    ]
    assert listing == [None, "2"]


async def test_malformed_commit_entries_are_dropped_and_counted(
    auth_clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    pages = [[commit(0), "not-a-commit-object", {"sha": "short", "commit": {}}]]
    client, _ = client_for(auth_clock, serving(commit_pages=pages))

    with caplog.at_level(logging.INFO, logger="adapters.github.client"):
        context = await client.fetch_pull_request(42, REPO, PR_NUMBER)

    assert len(context.commits) == 1
    record = next(r for r in caplog.records if r.message == "pull request fetched")
    assert record.dropped_commits == 2
    assert "not-a-commit-object" not in caplog.text
    assert "short" not in caplog.text


# --- the diff ------------------------------------------------------------


async def test_an_oversized_diff_is_rejected_without_leaking_it(
    auth_clock: FixedClock,
) -> None:
    marker = "LEAKED-DIFF-CONTENT"
    huge = marker + "x" * (5 * 1024 * 1024)
    client, _ = client_for(auth_clock, serving(diff_text=huge))

    with pytest.raises(ForgeUnavailableError) as caught:
        await client.fetch_pull_request(42, REPO, PR_NUMBER)

    assert marker not in str(caught.value)
    assert "xxxxx" not in str(caught.value)


async def test_an_oversized_content_length_is_rejected_without_reading_the_body(
    auth_clock: FixedClock,
) -> None:
    marker = "LEAKED-DIFF-CONTENT"
    inner = serving()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".diff"):
            return httpx.Response(
                200, headers={"Content-Length": str(5 * 1024 * 1024 + 1)}, text=marker
            )
        return inner(request)

    client, _ = client_for(auth_clock, handler)

    with pytest.raises(ForgeUnavailableError, match="diff exceeds") as caught:
        await client.fetch_pull_request(42, REPO, PR_NUMBER)

    assert marker not in str(caught.value)


async def test_an_oversized_diff_without_content_length_is_rejected_while_streaming(
    auth_clock: FixedClock,
) -> None:
    marker = "LEAKED-DIFF-CONTENT"
    huge = marker + "x" * (5 * 1024 * 1024)
    inner = serving()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".diff"):
            response = httpx.Response(200, text=huge)
            del response.headers["Content-Length"]
            return response
        return inner(request)

    client, _ = client_for(auth_clock, handler)

    with pytest.raises(ForgeUnavailableError) as caught:
        await client.fetch_pull_request(42, REPO, PR_NUMBER)

    assert "diff exceeds" in str(caught.value)
    assert marker not in str(caught.value)


async def test_a_diff_exactly_at_the_cap_is_accepted(auth_clock: FixedClock) -> None:
    client, _ = client_for(auth_clock, serving(diff_text="x" * (5 * 1024 * 1024)))

    context = await client.fetch_pull_request(42, REPO, PR_NUMBER)

    assert len(context.diff_text) == 5 * 1024 * 1024


# --- error mapping -------------------------------------------------------


async def test_a_missing_pull_request_is_not_found(auth_clock: FixedClock) -> None:
    client, _ = client_for(auth_clock, serving(detail_status=404))

    with pytest.raises(ForgeUnavailableError) as caught:
        await client.fetch_pull_request(42, REPO, PR_NUMBER)

    assert "not found" in str(caught.value)
    assert "secret-body-marker" not in str(caught.value)


async def test_a_forbidden_response_maps_to_permission_denied(
    auth_clock: FixedClock,
) -> None:
    client, _ = client_for(auth_clock, serving(detail_status=403))

    with pytest.raises(ForgeUnavailableError) as caught:
        await client.fetch_pull_request(42, REPO, PR_NUMBER)

    assert "permission" in str(caught.value).lower()
    assert "secret-body-marker" not in str(caught.value)
    assert TOKEN not in str(caught.value)


async def test_another_failure_is_forge_unavailable(auth_clock: FixedClock) -> None:
    client, _ = client_for(auth_clock, serving(detail_status=503))

    with pytest.raises(ForgeUnavailableError, match="HTTP 503"):
        await client.fetch_pull_request(42, REPO, PR_NUMBER)


async def test_a_network_failure_is_forge_unavailable(auth_clock: FixedClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json=github_fixture("installation_token"))
        raise httpx.ConnectError("unreachable", request=request)

    client, _ = client_for(auth_clock, handler)

    with pytest.raises(ForgeUnavailableError, match="network error"):
        await client.fetch_pull_request(42, REPO, PR_NUMBER)


async def test_a_timeout_is_forge_unavailable(auth_clock: FixedClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json=github_fixture("installation_token"))
        raise httpx.ReadTimeout("slow", request=request)

    client, _ = client_for(auth_clock, handler)

    with pytest.raises(ForgeUnavailableError, match="timed out"):
        await client.fetch_pull_request(42, REPO, PR_NUMBER)


# --- untrusted input into URLs -------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    ["", "..", "../web", "acme/../web", "/acme/web", "acme", "acme/web/x", "acme/"],
)
async def test_malformed_repo_names_are_rejected_before_any_http(
    auth_clock: FixedClock, bad_name: str
) -> None:
    client, transport = client_for(auth_clock, serving())

    with pytest.raises(ForgeUnavailableError) as caught:
        await client.fetch_pull_request(42, bad_name, PR_NUMBER)

    assert "invalid repo full name" in str(caught.value)
    if bad_name:
        assert bad_name not in str(caught.value)

    assert not transport.requests
