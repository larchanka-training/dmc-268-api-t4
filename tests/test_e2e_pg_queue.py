"""Two-process topology on real PostgreSQL: the API enqueues, the worker consumes.

docs/WORKFLOW_DESIGN.md §8 — two processes rather than one, so an API
restart never abandons an in-flight review. The app is wired the way
``api.main.build_app`` wires it when DATABASE_URL is set: engine +
``PostgresJobStore`` + ``dispatch_inline=False``. A signed delivery must
answer 202 and leave the job QUEUED with no forge traffic — the API process
did not process it — and the worker side is then driven explicitly
(``claim`` + ``process_claimed_job``, the same function the claim loop
runs) over the RecordingTransport fixture endpoints reused from
tests/test_e2e_context.py. Scratch-database pattern from
tests/worker/test_reaper.py (Alembic once per module, NullPool so
connections never cross the per-test event loops). No network, no sleeps.
The diff, PR title and description are sentinels: untrusted content must
never reach the logs (AGENTS.md hard rule 3).
"""

import asyncio
import json
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from adapters.db.repository import PostgresJobStore
from adapters.github.factory import build_vcs_client
from adapters.memory.auth import InMemorySessionStore, InMemorySignInAttemptStore
from adapters.memory.repositories import InMemoryRepositoriesCache
from api.main import create_app
from domain.ports import GitProvider, ReviewJobStatus
from domain.repositories import RepositoriesService
from tests.conftest import (
    APP_SLUG,
    TEST_ORIGIN,
    TEST_WEBHOOK_SECRET,
    FakeIdentityProvider,
    FakeInstallationGateway,
    FixedClock,
    RecordingTransport,
    make_settings,
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
    DELIVERY,
    DESCRIPTION_SENTINEL,
    DIFF_SENTINEL,
    EXPECTED_JOB,
    EXPECTED_STATS,
    PR_NUMBER,
    REPO,
    TITLE_SENTINEL,
    forge_handler,
    github_settings,
    opened_payload,
)
from worker.process import process_claimed_job

E2E_QUEUE_DB_NAME = "dmc268_e2e_queue_test"
NEW_SHA = "9f8e7d6c5b" * 4


@dataclass
class PgQueueE2E:
    """One test's wiring: PG-mode app, store, engine and the recording forge."""

    app: Any
    engine: AsyncEngine
    store: PostgresJobStore
    transport: RecordingTransport
    vcs: GitProvider


@pytest.fixture(scope="module")
def queue_db_url() -> Iterator[str]:
    base = make_url(os.environ.get("DATABASE_URL") or COMPOSE_URL)
    admin_dsn = require_postgres(
        base, reason="the PG queue e2e webhook flow needs a local PostgreSQL"
    )
    create_scratch_database(admin_dsn, E2E_QUEUE_DB_NAME)
    url = base.set(database=E2E_QUEUE_DB_NAME).render_as_string(hide_password=False)
    run_alembic(url, "upgrade", "head")
    yield url
    drop_scratch_database(admin_dsn, E2E_QUEUE_DB_NAME)


@pytest.fixture
def engine(queue_db_url: str) -> Iterator[AsyncEngine]:
    # NullPool: connections must never cross the per-test event loops.
    made = create_async_engine(queue_db_url, poolclass=NullPool)
    yield made
    asyncio.run(made.dispose())


@pytest.fixture
def pg_e2e(engine: AsyncEngine, auth_clock: FixedClock) -> PgQueueE2E:
    async def truncate() -> None:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE TABLE pr_review_jobs CASCADE"))

    asyncio.run(truncate())
    store = PostgresJobStore(engine)
    transport = RecordingTransport(forge_handler)
    vcs = build_vcs_client(github_settings(), transport=transport, clock=auth_clock)
    app = create_app(
        settings=make_settings(),
        identity_provider=FakeIdentityProvider(),
        session_store=InMemorySessionStore(auth_clock),
        attempt_store=InMemorySignInAttemptStore(auth_clock),
        clock=auth_clock,
        repositories=RepositoriesService(
            gateway=FakeInstallationGateway(),
            cache=InMemoryRepositoriesCache(auth_clock),
            clock=auth_clock,
            cache_ttl=timedelta(seconds=60),
        ),
        app_slug=APP_SLUG,
        jobs=store,
        vcs=vcs,
        webhook_secret=TEST_WEBHOOK_SECRET,
        dispatch_inline=False,
    )
    return PgQueueE2E(app=app, engine=engine, store=store, transport=transport, vcs=vcs)


async def post_opened(
    app: Any, delivery: str, payload: dict[str, Any] | None = None
) -> httpx.Response:
    """POST one signed `pull_request.opened` delivery with this delivery id."""
    body = json.dumps(payload or opened_payload()).encode("utf-8")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=TEST_ORIGIN
    ) as client:
        return await client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sign_webhook(body),
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": delivery,
            },
        )


async def count_jobs(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT count(*) FROM pr_review_jobs"))
        return int(result.scalar_one())


async def test_the_api_only_enqueues_and_the_worker_side_completes_the_job(
    pg_e2e: PgQueueE2E,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The diff-in-logs dev flag must not leak from a developer's shell into
    # this rule-3 assertion.
    monkeypatch.delenv("DEBUG_LOG_DIFF", raising=False)
    caplog.set_level(logging.DEBUG)

    response = await post_opened(pg_e2e.app, DELIVERY)

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    job_id = response.json()["job_id"]

    # The API process did not process: QUEUED in PostgreSQL, and the forge
    # was never touched.
    stored = await pg_e2e.store.get(job_id)
    assert stored is not None
    assert stored.status is ReviewJobStatus.QUEUED
    assert stored.payload == EXPECTED_JOB
    assert pg_e2e.transport.requests == []

    # The worker side: claim, then the same processor the claim loop runs.
    claimed = await pg_e2e.store.claim(worker_id="test-worker")
    assert claimed is not None
    assert claimed.payload == EXPECTED_JOB
    assert await process_claimed_job(claimed, pg_e2e.vcs, pg_e2e.store) is True

    done = await pg_e2e.store.get(job_id)
    assert done is not None
    assert done.status is ReviewJobStatus.COMPLETED
    assert done.stats == EXPECTED_STATS

    # The worker drove the forge exactly: one installation token, then the
    # pull request, its commits, and the whole-range diff.
    paths = [request.url.path for request in pg_e2e.transport.requests]
    assert len(paths) == 4
    assert sum(path.endswith("/access_tokens") for path in paths) == 1
    assert any(path.endswith(f"/repos/{REPO}/pulls/{PR_NUMBER}") for path in paths)
    assert any(path.endswith(f"/repos/{REPO}/pulls/{PR_NUMBER}/commits") for path in paths)
    assert any(path.endswith(f"/repos/{REPO}/pulls/{PR_NUMBER}.diff") for path in paths)

    # Redelivery: still 202, still exactly one row, still COMPLETED.
    duplicate = await post_opened(pg_e2e.app, DELIVERY)
    assert duplicate.status_code == 202
    assert duplicate.json() == {"status": "duplicate"}
    assert await count_jobs(pg_e2e.engine) == 1
    final = await pg_e2e.store.get(job_id)
    assert final is not None
    assert final.status is ReviewJobStatus.COMPLETED

    # Untrusted content — diff text, PR title, description — never reaches
    # the logs (AGENTS.md hard rule 3).
    for sentinel in (TITLE_SENTINEL, DESCRIPTION_SENTINEL, DIFF_SENTINEL):
        assert sentinel not in caplog.text


async def test_a_newer_delivery_for_the_same_pr_supersedes_the_first_before_claim(
    pg_e2e: PgQueueE2E,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEBUG_LOG_DIFF", raising=False)
    caplog.set_level(logging.DEBUG)
    newer_payload = opened_payload()
    newer_payload["pull_request"]["head"]["sha"] = NEW_SHA

    first = await post_opened(pg_e2e.app, "e2e-pg-delivery-1")
    second = await post_opened(pg_e2e.app, "e2e-pg-delivery-2", newer_payload)

    assert first.status_code == 202
    assert second.status_code == 202

    # The first job is SUPERSEDED by the second enqueue, before any claim.
    superseded = await pg_e2e.store.get(first.json()["job_id"])
    assert superseded is not None
    assert superseded.status is ReviewJobStatus.SUPERSEDED

    # Claim hands out only the newer job.
    claimed = await pg_e2e.store.claim(worker_id="test-worker")
    assert claimed is not None
    assert claimed.job_id == second.json()["job_id"]
    assert claimed.payload.head_sha == NEW_SHA

    for sentinel in (TITLE_SENTINEL, DESCRIPTION_SENTINEL, DIFF_SENTINEL):
        assert sentinel not in caplog.text
