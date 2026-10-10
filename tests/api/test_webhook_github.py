"""The GitHub webhook endpoint over ASGITransport, with fake ports.

Covers docs/WORKFLOW_DESIGN.md §2 Steps 2–3 at the API boundary: size cap,
HMAC verification over exactly the received bytes, event routing, idempotent
enqueue, and the background processing task — drained, never slept on. The
app runs on the test's own event loop (httpx ASGITransport), so awaiting
`drain_background_tasks` observes the job's final state deterministically.
"""

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest

from adapters.jobs.memory import MemoryJobStore
from api.webhooks.github import MAX_BODY, drain_background_tasks
from domain.ports import (
    CommitInfo,
    JobStats,
    NewReviewJob,
    PullRequestContext,
    ReviewJob,
    ReviewJobStatus,
)
from tests.conftest import (
    TEST_ORIGIN,
    TEST_WEBHOOK_SECRET,
    FakeIdentityProvider,
    FixedClock,
    build_test_app,
    sign_webhook,
)

HEAD_SHA = "a1b2c3d4e5" * 4
BASE_SHA = "f6e5d4c3b2" * 4
REPO = "acme/web"
REPO_ID = 751065667
INSTALLATION_ID = 42
DELIVERY = "d-abc123"

SOURCE_DIFF = """diff --git a/app/users.py b/app/users.py
index 1111111..2222222 100644
--- a/app/users.py
+++ b/app/users.py
@@ -1,3 +1,4 @@
 base
+import new
 def main():
-    pass
+    return 1
"""


def sign(body: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    return sign_webhook(body, secret)


def opened_payload(action: str = "opened") -> dict[str, Any]:
    return {
        "action": action,
        "installation": {"id": INSTALLATION_ID},
        "repository": {"id": REPO_ID, "full_name": REPO},
        "pull_request": {
            "number": 7,
            "head": {"sha": HEAD_SHA},
            "base": {"sha": BASE_SHA},
            "title": "Add payments webhook",
            "user": {"login": "octocat", "id": 583231},
            "author_association": "CONTRIBUTOR",
            "draft": False,
        },
    }


def expected_job() -> NewReviewJob:
    return NewReviewJob(
        provider="github",
        delivery_id=DELIVERY,
        installation_id=INSTALLATION_ID,
        repo_id=REPO_ID,
        repo_full_name=REPO,
        pr_number=7,
        head_sha=HEAD_SHA,
        base_sha=BASE_SHA,
        event_action="opened",
    )


class HeldJobStore:
    """A JobRepository that parks mark_processing until released.

    A test can then assert QUEUED after the 202 without racing the
    background task, and release it to watch the run finish. Also counts
    the jobs it created, so a redelivery's dedup is observable.
    """

    def __init__(self, clock: FixedClock) -> None:
        self.inner = MemoryJobStore(clock=clock)
        self.hold = asyncio.Event()
        self.created: list[str] = []

    async def enqueue(self, job: NewReviewJob) -> str | None:
        job_id = await self.inner.enqueue(job)
        if job_id is not None:
            self.created.append(job_id)
        return job_id

    async def mark_processing(self, job_id: str) -> bool:
        await self.hold.wait()
        return await self.inner.mark_processing(job_id)

    async def mark_completed(self, job_id: str, stats: JobStats) -> bool:
        return await self.inner.mark_completed(job_id, stats)

    async def mark_failed(self, job_id: str, error_kind: str) -> bool:
        return await self.inner.mark_failed(job_id, error_kind)

    async def get(self, job_id: str) -> ReviewJob | None:
        return await self.inner.get(job_id)


class FakeGitProvider:
    """Returns one fixed pull request context; records the calls."""

    def __init__(self, context: PullRequestContext) -> None:
        self.context = context
        self.calls: list[tuple[int, str, int]] = []

    async def fetch_pull_request(
        self, installation_id: int, repo_full_name: str, number: int
    ) -> PullRequestContext:
        self.calls.append((installation_id, repo_full_name, number))
        return self.context


def make_context() -> PullRequestContext:
    return PullRequestContext(
        installation_id=INSTALLATION_ID,
        repo_full_name=REPO,
        pr_number=7,
        title="Add payments webhook",
        description="Handles provider retries.",
        head_sha=HEAD_SHA,
        base_sha=BASE_SHA,
        head_ref="octocat/payments",
        base_ref="main",
        author_login="octocat",
        author_external_id=583231,
        author_association="CONTRIBUTOR",
        commits=(CommitInfo(sha=HEAD_SHA, message="one", author_login="octocat"),),
        diff_text=SOURCE_DIFF,
    )


def opened_body(action: str = "opened") -> bytes:
    return json.dumps(opened_payload(action)).encode("utf-8")


async def post_delivery(
    app: Any,
    body: bytes,
    headers: dict[str, str] | None = None,
    *,
    signed: bool = True,
    event: str | None = "pull_request",
    delivery: str | None = DELIVERY,
) -> httpx.Response:
    """POST a delivery; by default it is signed and routed as pull_request."""
    merged: dict[str, str] = {"X-Hub-Signature-256": sign(body)} if signed else {}
    if event is not None:
        merged["X-GitHub-Event"] = event
    if delivery is not None:
        merged["X-GitHub-Delivery"] = delivery
    merged.update(headers or {})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=TEST_ORIGIN
    ) as client:
        return await client.post("/webhooks/github", content=body, headers=merged)


# --- Step 2: signature and size gate -------------------------------------


async def test_a_delivery_without_a_signature_is_unauthorized(
    auth_clock: FixedClock,
) -> None:
    app = build_test_app(auth_clock, FakeIdentityProvider())

    response = await post_delivery(app, opened_body(), signed=False)

    assert response.status_code == 401
    assert response.json() == {"error": "invalid_signature"}


async def test_a_wrong_signature_is_unauthorized(auth_clock: FixedClock) -> None:
    app = build_test_app(auth_clock, FakeIdentityProvider())
    forged = sign(opened_body(), secret="whsec-attacker")

    response = await post_delivery(app, opened_body(), headers={"X-Hub-Signature-256": forged})

    assert response.status_code == 401
    assert response.json() == {"error": "invalid_signature"}


async def test_a_tampered_body_does_not_match_the_signature(
    auth_clock: FixedClock,
) -> None:
    app = build_test_app(auth_clock, FakeIdentityProvider())
    over_other_bytes = sign(b'{"action": "opened"}')

    response = await post_delivery(
        app, opened_body(), headers={"X-Hub-Signature-256": over_other_bytes}
    )

    assert response.status_code == 401


async def test_a_body_over_the_cap_is_rejected_before_anything_else(
    auth_clock: FixedClock,
) -> None:
    app = build_test_app(auth_clock, FakeIdentityProvider())
    oversized = b"a" * (MAX_BODY + 1)

    response = await post_delivery(app, oversized, signed=False)

    assert response.status_code == 413
    assert response.json() == {"error": "payload_too_large"}


async def test_a_declared_length_over_the_cap_is_rejected_before_reading_the_body(
    auth_clock: FixedClock,
) -> None:
    app = build_test_app(auth_clock, FakeIdentityProvider())

    # The declared Content-Length is over the cap although the actual body
    # is tiny: the 413 must come from the header alone, before request.body().
    response = await post_delivery(
        app,
        b"{}",
        headers={"Content-Length": str(MAX_BODY + 1)},
        signed=False,
    )

    assert response.status_code == 413
    assert response.json() == {"error": "payload_too_large"}


# --- Step 2: parsing and routing ------------------------------------------


async def test_a_body_that_is_not_json_is_rejected(auth_clock: FixedClock) -> None:
    app = build_test_app(auth_clock, FakeIdentityProvider())

    response = await post_delivery(app, b"{not json")

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_json"}


async def test_json_that_is_not_an_object_is_rejected(auth_clock: FixedClock) -> None:
    app = build_test_app(auth_clock, FakeIdentityProvider())

    response = await post_delivery(app, b"[1, 2]")

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_payload"}


@pytest.mark.parametrize("event", ["ping", "forks", "installation"])
async def test_non_pull_request_events_are_acknowledged(auth_clock: FixedClock, event: str) -> None:
    app = build_test_app(auth_clock, FakeIdentityProvider())

    # The body is a reviewable pull_request payload: the event header routes
    # before the action (Step 2 point 4).
    response = await post_delivery(app, opened_body(), event=event)

    assert response.status_code == 204


async def test_a_delivery_without_an_event_header_is_acknowledged(
    auth_clock: FixedClock,
) -> None:
    app = build_test_app(auth_clock, FakeIdentityProvider())

    response = await post_delivery(app, opened_body(), event=None)

    assert response.status_code == 204


async def test_a_non_reviewable_action_is_acknowledged(auth_clock: FixedClock) -> None:
    app = build_test_app(auth_clock, FakeIdentityProvider())

    response = await post_delivery(app, opened_body(action="edited"))

    assert response.status_code == 204


async def test_a_malformed_pull_request_payload_is_rejected(
    auth_clock: FixedClock,
) -> None:
    store = HeldJobStore(auth_clock)
    app = build_test_app(auth_clock, FakeIdentityProvider(), jobs=store)
    payload = opened_payload()
    del payload["pull_request"]

    response = await post_delivery(app, json.dumps(payload).encode("utf-8"))

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_payload"}
    assert store.created == []


# --- Step 3: enqueue and the background run -------------------------------


async def test_an_opened_pull_request_is_queued_then_processed(
    auth_clock: FixedClock,
) -> None:
    store = HeldJobStore(auth_clock)
    vcs = FakeGitProvider(make_context())
    app = build_test_app(auth_clock, FakeIdentityProvider(), jobs=store, vcs=vcs)

    response = await post_delivery(app, opened_body())

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    job_id = body["job_id"]

    # Held at the first status transition: QUEUED, with the exact payload.
    job = await store.get(job_id)
    assert job is not None
    assert job.status is ReviewJobStatus.QUEUED
    assert job.payload == expected_job()

    store.hold.set()
    await drain_background_tasks()

    done = await store.get(job_id)
    assert done is not None
    assert done.status is ReviewJobStatus.COMPLETED
    assert vcs.calls == [(INSTALLATION_ID, REPO, 7)]
    assert done.stats == JobStats(
        files_total=1,
        files_reviewable=1,
        hunks_total=1,
        chunks_total=1,
        skipped_counts=(),
    )


async def test_a_redelivered_delivery_is_success_without_a_second_job(
    auth_clock: FixedClock,
) -> None:
    store = HeldJobStore(auth_clock)
    app = build_test_app(
        auth_clock,
        FakeIdentityProvider(),
        jobs=store,
        vcs=FakeGitProvider(make_context()),
    )
    body = opened_body()

    first = await post_delivery(app, body)
    second = await post_delivery(app, body)

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json() == {"status": "duplicate"}
    assert len(store.created) == 1

    store.hold.set()
    await drain_background_tasks()

    job = await store.get(store.created[0])
    assert job is not None
    assert job.status is ReviewJobStatus.COMPLETED


# --- no CSRF, no leaks -----------------------------------------------------


async def test_the_webhook_needs_neither_csrf_headers_nor_a_session(
    auth_clock: FixedClock,
) -> None:
    app = build_test_app(auth_clock, FakeIdentityProvider())

    # No X-Requested-With, no Origin, no cookies — a forge delivery carries
    # none of them; every other unsafe route would answer 403 here.
    response = await post_delivery(app, opened_body(), event="ping")

    assert response.status_code == 204


async def test_the_secret_never_reaches_logs_or_responses(
    auth_clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    app = build_test_app(auth_clock, FakeIdentityProvider())
    forged = sign(opened_body(), secret="whsec-attacker")

    response = await post_delivery(app, opened_body(), headers={"X-Hub-Signature-256": forged})

    assert response.status_code == 401
    assert TEST_WEBHOOK_SECRET not in response.text
    assert TEST_WEBHOOK_SECRET not in caplog.text


# --- routing logs: every verified delivery is traceable ---------------------


async def test_an_accepted_delivery_logs_the_event_and_pull_request(
    auth_clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    store = HeldJobStore(auth_clock)
    app = build_test_app(
        auth_clock,
        FakeIdentityProvider(),
        jobs=store,
        vcs=FakeGitProvider(make_context()),
    )

    with caplog.at_level(logging.INFO, logger="api.webhooks.github"):
        response = await post_delivery(app, opened_body())

    assert response.status_code == 202
    text = caplog.text
    assert "github webhook accepted: event=pull_request action=opened" in text
    assert f"delivery={DELIVERY}" in text
    assert f"repo={REPO} pr=7" in text
    assert f"installation={INSTALLATION_ID}" in text
    assert f"head={HEAD_SHA[:7]}" in text
    assert f"base={BASE_SHA[:7]}" in text


async def test_ignored_events_and_actions_are_logged(
    auth_clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    app = build_test_app(auth_clock, FakeIdentityProvider())

    with caplog.at_level(logging.INFO, logger="api.webhooks.github"):
        await post_delivery(app, opened_body(), event="ping")
        await post_delivery(app, opened_body(action="edited"))

    text = caplog.text
    assert "github webhook ignored: event=ping" in text
    assert "github webhook ignored: event=pull_request action=edited" in text


async def test_a_duplicate_delivery_is_logged(
    auth_clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    store = HeldJobStore(auth_clock)
    app = build_test_app(
        auth_clock,
        FakeIdentityProvider(),
        jobs=store,
        vcs=FakeGitProvider(make_context()),
    )
    body = opened_body()

    with caplog.at_level(logging.INFO, logger="api.webhooks.github"):
        await post_delivery(app, body)
        await post_delivery(app, body)

    assert "github webhook duplicate delivery: action=opened" in caplog.text
