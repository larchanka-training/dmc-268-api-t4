"""Published findings end to end on real PostgreSQL: webhook → queue → worker → API.

A signed delivery is enqueued by the API; the worker side runs the review stage with
a fake model and the real `GitHubReviewPublisher` over a recording forge, then the
console reads the job back over `GET /jobs/{job_id}`. A second push to the same pull
request posts nothing it already posted. No network, no sleeps (AGENTS.md rule 4).
"""

import asyncio
import json
import os
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from adapters.db.repository import PostgresJobStore
from adapters.db.review_comments import PostgresReviewCommentStore
from adapters.github.app_auth import GitHubAppAuth
from adapters.github.factory import build_vcs_client
from adapters.github.publisher import GitHubReviewPublisher
from adapters.memory.auth import InMemorySessionStore, InMemorySignInAttemptStore
from adapters.memory.repositories import InMemoryRepositoriesCache
from api.main import create_app
from domain.models import Category, DiffSide, Finding, FindingPosition, Severity
from domain.ports import Profile, ReviewJobStatus
from domain.repositories import RepositoriesService
from tests.conftest import (
    APP_SLUG,
    TEST_ORIGIN,
    TEST_WEBHOOK_SECRET,
    FakeIdentityProvider,
    FakeInstallationGateway,
    FixedClock,
    RecordingTransport,
    github_fixture,
    make_organization,
    make_settings,
    make_user,
    sign_webhook,
)
from tests.db.support import (
    COMPOSE_URL,
    create_scratch_database,
    drop_scratch_database,
    require_postgres,
    run_alembic,
)
from tests.test_e2e_context import (
    INSTALLATION_ID,
    PR_NUMBER,
    REPO,
    forge_handler,
    github_settings,
    opened_payload,
)
from tests.worker.test_review_stage import FakeGateway
from worker.process import ReviewStage, process_claimed_job

DB_NAME = "dmc268_e2e_findings_test"
NEW_SHA = "9f8e7d6c5b" * 4
REVIEW_ID = 900


class FakeForge:
    """The fetch endpoints of tests/test_e2e_context.py plus what the publisher calls.

    A created review's inline comments are listed back with ids, as GitHub does."""

    def __init__(self) -> None:
        self.reviews: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/check-runs" in path:
            return httpx.Response(201, json={"id": 555, "html_url": "https://github.test/runs/5"})
        if request.method == "POST" and path.endswith(f"/pulls/{PR_NUMBER}/reviews"):
            self.reviews.append(json.loads(request.content))
            return httpx.Response(200, json={"id": REVIEW_ID, "html_url": "https://github.test/r"})
        if path.endswith(f"/reviews/{REVIEW_ID}/comments"):
            posted = self.reviews[-1].get("comments", [])
            return httpx.Response(
                200,
                json=[
                    {"id": 7000 + index, "body": comment["body"], "html_url": "https://x.test"}
                    for index, comment in enumerate(posted)
                ],
            )
        if path.endswith("/access_tokens"):
            return httpx.Response(200, json=github_fixture("installation_token"))
        return forge_handler(request)


@pytest.fixture(scope="module")
def db_url() -> Iterator[str]:
    base = make_url(os.environ.get("DATABASE_URL") or COMPOSE_URL)
    admin_dsn = require_postgres(base, reason="the findings e2e flow needs a local PostgreSQL")
    create_scratch_database(admin_dsn, DB_NAME)
    url = base.set(database=DB_NAME).render_as_string(hide_password=False)
    run_alembic(url, "upgrade", "head")
    yield url
    drop_scratch_database(admin_dsn, DB_NAME)


@pytest.fixture
def engine(db_url: str) -> Iterator[AsyncEngine]:
    # NullPool: connections must never cross the per-test event loops.
    made = create_async_engine(db_url, poolclass=NullPool)

    async def truncate() -> None:
        async with made.begin() as conn:
            await conn.execute(text("TRUNCATE TABLE pr_review_jobs CASCADE"))

    asyncio.run(truncate())
    yield made
    asyncio.run(made.dispose())


def build(engine: AsyncEngine, clock: FixedClock, transport: RecordingTransport) -> Any:
    organization = make_organization("acme", "100", installation_id=INSTALLATION_ID)
    return create_app(
        settings=make_settings(),
        identity_provider=FakeIdentityProvider(
            Profile(user=make_user(), organizations=(organization,))
        ),
        session_store=InMemorySessionStore(clock),
        attempt_store=InMemorySignInAttemptStore(clock),
        clock=clock,
        repositories=RepositoriesService(
            gateway=FakeInstallationGateway(),
            cache=InMemoryRepositoriesCache(clock),
            clock=clock,
            cache_ttl=timedelta(seconds=60),
        ),
        app_slug=APP_SLUG,
        jobs=PostgresJobStore(engine),
        vcs=build_vcs_client(github_settings(), transport=transport, clock=clock),
        webhook_secret=TEST_WEBHOOK_SECRET,
        dispatch_inline=False,
        engine=engine,
        findings=PostgresReviewCommentStore(engine),
    )


async def deliver(client: httpx.AsyncClient, delivery: str, head_sha: str | None = None) -> str:
    payload = opened_payload()
    if head_sha is not None:
        payload["pull_request"]["head"]["sha"] = head_sha
    body = json.dumps(payload).encode("utf-8")
    response = await client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": sign_webhook(body),
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": delivery,
        },
    )
    assert response.status_code == 202
    return str(response.json()["job_id"])


async def sign_in(client: httpx.AsyncClient) -> None:
    start = await client.get("/auth/github")
    state = str(httpx.URL(start.headers["location"]).params["state"])
    await client.get("/auth/github/callback", params={"state": state, "code": "c"})


async def test_published_findings_reach_the_console_and_are_not_posted_twice(
    engine: AsyncEngine, auth_clock: FixedClock
) -> None:
    forge = FakeForge()
    transport = RecordingTransport(forge)
    app = build(engine, auth_clock, transport)
    jobs = PostgresJobStore(engine)
    config = github_settings()
    stage = ReviewStage(
        # Line 4 of app/payments.py is an added line in the e2e diff.
        gateway=FakeGateway(
            (
                Finding(
                    position=FindingPosition(path="app/payments.py", line=4, side=DiffSide.NEW),
                    severity=Severity.HIGH,
                    category=Category.CORRECTNESS,
                    message="The charge result is ignored.",
                    suggestion=None,
                ),
            )
        ),
        publisher=GitHubReviewPublisher(
            config, GitHubAppAuth(config, auth_clock, transport=transport), transport=transport
        ),
        comments=PostgresReviewCommentStore(engine),
    )
    vcs = build_vcs_client(config, transport=transport, clock=auth_clock)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=TEST_ORIGIN
    ) as client:
        first_id = await deliver(client, "findings-1")
        claimed = await jobs.claim(worker_id="w-1")
        assert claimed is not None
        assert await process_claimed_job(claimed, vcs, jobs, review=stage) is True

        await sign_in(client)
        detail = (await client.get(f"/jobs/{first_id}")).json()

        assert detail["status"] == "COMPLETED"
        assert detail["finding_count"] == 1
        [found] = detail["findings"]
        assert (found["path"], found["line"], found["side"]) == ("app/payments.py", 4, "new")
        assert (found["severity"], found["category"]) == ("high", "correctness")
        assert "The charge result is ignored." in found["message"]
        assert found["message"] == forge.reviews[0]["comments"][0]["body"]
        assert found["external_url"] == (
            f"https://github.com/{REPO}/pull/{PR_NUMBER}#discussion_r7000"
        )

        # A push: a new job on the same pull request, the model says the same thing.
        second_id = await deliver(client, "findings-2", head_sha=NEW_SHA)
        claimed = await jobs.claim(worker_id="w-1")
        assert claimed is not None and claimed.job_id == second_id
        assert await process_claimed_job(claimed, vcs, jobs, review=stage) is True

        again = (await client.get(f"/jobs/{second_id}")).json()

    assert again["status"] == str(ReviewJobStatus.COMPLETED)
    assert again["findings"] == []
    assert all(not review.get("comments") for review in forge.reviews[1:])
