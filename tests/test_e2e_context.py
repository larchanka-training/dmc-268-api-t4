"""DoD demonstration: webhook → validate → download → parse → structure recorded.

One signed `pull_request` delivery drives the real endpoint wiring, a real
GitHubVCSClient over a RecordingTransport serving the token, pull-request,
commits and .diff endpoints, and the in-memory job queue. The queue is
in-memory now and PostgreSQL later (tasks/plan.md decision 1, 2026-10-07);
the port stays, so this test stays. No network, no sleeps: the background
task is drained. The diff, PR title and description are sentinels, so the
caplog assertions prove untrusted content never reaches the logs
(AGENTS.md hard rule 3).
"""

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from adapters.github.config import GitHubAppSettings
from adapters.github.factory import build_vcs_client
from adapters.jobs.memory import MemoryJobStore
from api.webhooks.github import drain_background_tasks
from domain.ports import JobStats, NewReviewJob, ReviewJobStatus
from tests.conftest import (
    TEST_ORIGIN,
    TEST_WEBHOOK_SECRET,
    FakeIdentityProvider,
    FixedClock,
    RecordingTransport,
    build_test_app,
    github_fixture,
    sign_webhook,
)

KEY_PATH = Path(__file__).resolve().parent / "fixtures" / "github" / "app_key.pem"

REPO = "acme/web"
REPO_ID = 751065667
PR_NUMBER = 7
INSTALLATION_ID = 42
HEAD_SHA = "a1b2c3d4e5" * 4
BASE_SHA = "0f1e2d3c4b" * 4
DELIVERY = "e2e-delivery-1"

TITLE_SENTINEL = "e2e-title-SENTINEL-71a1"
DESCRIPTION_SENTINEL = "e2e-description-SENTINEL-71a2"
DIFF_SENTINEL = "e2e-diff-SENTINEL-71a3"

# One reviewable source file, one lockfile, one binary: the exact mix the
# filter is supposed to split.
DIFF_TEXT = f"""diff --git a/app/payments.py b/app/payments.py
index 1111111..2222222 100644
--- a/app/payments.py
+++ b/app/payments.py
@@ -1,3 +1,4 @@
 import stripe
+{DIFF_SENTINEL}
 def charge(amount: int) -> None:
-    pass
+    stripe.Charge(amount)
diff --git a/package-lock.json b/package-lock.json
index 3333333..4444444 100644
--- a/package-lock.json
+++ b/package-lock.json
@@ -1,3 +1,3 @@
 {{
-  "a": 1
+  "a": 2
 }}
diff --git a/assets/logo.png b/assets/logo.png
index 5555555..6666666 100644
Binary files a/assets/logo.png and b/assets/logo.png differ
"""

PULL_DETAIL: dict[str, Any] = {
    "number": PR_NUMBER,
    "title": TITLE_SENTINEL,
    "body": DESCRIPTION_SENTINEL,
    "user": {"login": "octocat", "id": 583231},
    "author_association": "CONTRIBUTOR",
    "head": {"sha": HEAD_SHA, "ref": "octocat/payments"},
    "base": {"sha": BASE_SHA, "ref": "main"},
}

COMMIT: dict[str, Any] = {
    "sha": HEAD_SHA,
    "commit": {"message": "Add payments"},
    "author": {"login": "octocat"},
}

EXPECTED_JOB = NewReviewJob(
    provider="github",
    delivery_id=DELIVERY,
    installation_id=INSTALLATION_ID,
    repo_id=REPO_ID,
    repo_full_name=REPO,
    pr_number=PR_NUMBER,
    head_sha=HEAD_SHA,
    base_sha=BASE_SHA,
    event_action="opened",
    pr_title=TITLE_SENTINEL,
    author_login="octocat",
    head_ref="octocat/payments",
    base_ref="main",
)

EXPECTED_STATS = JobStats(
    files_total=3,
    files_reviewable=1,
    hunks_total=2,
    chunks_total=1,
    skipped_counts=(("binary", 1), ("lockfile", 1)),
)


def github_settings() -> GitHubAppSettings:
    return GitHubAppSettings(
        app_id="123456",
        private_key=KEY_PATH.read_text(encoding="utf-8"),
        app_slug="review-agent",
        webhook_secret=TEST_WEBHOOK_SECRET,
        api_base_url="https://api.github.test",
    )


def forge_handler(request: httpx.Request) -> httpx.Response:
    """One handler serving the four endpoints the review fetch touches."""
    path = request.url.path
    if path.endswith("/access_tokens"):
        return httpx.Response(200, json=github_fixture("installation_token"))
    if path.endswith(f"/repos/{REPO}/pulls/{PR_NUMBER}"):
        return httpx.Response(200, json=PULL_DETAIL)
    if path.endswith(f"/repos/{REPO}/pulls/{PR_NUMBER}/commits"):
        return httpx.Response(200, json=[COMMIT])
    if path.endswith(f"/repos/{REPO}/pulls/{PR_NUMBER}.diff"):
        return httpx.Response(200, text=DIFF_TEXT)
    raise AssertionError(f"unexpected request for {request.url}")


def opened_payload() -> dict[str, Any]:
    return {
        "action": "opened",
        "installation": {"id": INSTALLATION_ID},
        "repository": {"id": REPO_ID, "full_name": REPO},
        "pull_request": {
            "number": PR_NUMBER,
            "head": {"sha": HEAD_SHA, "ref": "octocat/payments"},
            "base": {"sha": BASE_SHA, "ref": "main"},
            "title": TITLE_SENTINEL,
            "user": {"login": "octocat", "id": 583231},
            "author_association": "CONTRIBUTOR",
            "draft": False,
        },
    }


async def test_a_signed_delivery_ends_recorded_with_exact_structure(
    auth_clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The diff-in-logs dev flag must not leak from a developer's shell into
    # this rule-3 assertion.
    monkeypatch.delenv("DEBUG_LOG_DIFF", raising=False)
    caplog.set_level(logging.DEBUG)
    transport = RecordingTransport(forge_handler)
    jobs = MemoryJobStore(clock=auth_clock)
    vcs = build_vcs_client(github_settings(), transport=transport, clock=auth_clock)
    app = build_test_app(auth_clock, FakeIdentityProvider(), jobs=jobs, vcs=vcs)

    body = json.dumps(opened_payload()).encode("utf-8")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=TEST_ORIGIN
    ) as client:
        response = await client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sign_webhook(body),
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": DELIVERY,
            },
        )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    job_id = response.json()["job_id"]

    await drain_background_tasks()

    job = await jobs.get(job_id)
    assert job is not None
    assert job.status is ReviewJobStatus.COMPLETED
    assert job.payload == EXPECTED_JOB
    assert job.stats == EXPECTED_STATS

    # The forge was driven exactly: one installation token, then the pull
    # request, its commits, and the whole-range diff.
    paths = [request.url.path for request in transport.requests]
    assert len(paths) == 4
    assert sum(path.endswith("/access_tokens") for path in paths) == 1
    assert any(path.endswith(f"/repos/{REPO}/pulls/{PR_NUMBER}") for path in paths)
    assert any(path.endswith(f"/repos/{REPO}/pulls/{PR_NUMBER}/commits") for path in paths)
    assert any(path.endswith(f"/repos/{REPO}/pulls/{PR_NUMBER}.diff") for path in paths)

    # Untrusted content — diff text, PR title, description — never reaches
    # the logs (AGENTS.md hard rule 3).
    for sentinel in (TITLE_SENTINEL, DESCRIPTION_SENTINEL, DIFF_SENTINEL):
        assert sentinel not in caplog.text
