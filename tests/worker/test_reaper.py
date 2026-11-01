"""The lease reaper on real PostgreSQL: PostgresJobStore.reap_expired and
reap_tick under the advisory lock.

Scratch-database pattern from tests/ports/test_job_repository.py (Alembic
migrated once per module, NullPool so connections never cross the
per-test event loops). No sleeps: leases are expired by writing
``locked_until = now() - interval '1 second'``, and retries are made due
the same way, because the SQL store's clock is the database's now().
"""

import asyncio
import os
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from adapters.db.dsn import to_asyncpg
from adapters.db.repository import PostgresJobStore
from adapters.observability import metrics
from domain.jobs import DEFAULT_MAX_RETRIES
from domain.ports import JobStats, NewReviewJob, ReviewJobStatus
from tests.db.support import (
    COMPOSE_URL,
    create_scratch_database,
    drop_scratch_database,
    require_postgres,
    run_alembic,
)
from worker.periodic import TASK_KEYS, reap_tick

WORKER_DB_NAME = "dmc268_worker_test"
HEAD_SHA = "a1b2c3d4e5" * 4
BASE_SHA = "f6e5d4c3b2" * 4


def make_job(delivery_id: str) -> NewReviewJob:
    return NewReviewJob(
        provider="github",
        delivery_id=delivery_id,
        installation_id=42,
        repo_id=751065667,
        repo_full_name="octocat/hello-world",
        pr_number=7,
        head_sha=HEAD_SHA,
        base_sha=BASE_SHA,
        event_action="opened",
        pr_title="Add a feature",
    )


async def enqueue(store: PostgresJobStore, delivery_id: str) -> str:
    job_id = await store.enqueue(make_job(delivery_id))
    assert job_id is not None
    return job_id


@pytest.fixture(scope="module")
def worker_db() -> Iterator[tuple[str, str]]:
    base = make_url(os.environ.get("DATABASE_URL") or COMPOSE_URL)
    admin_dsn = require_postgres(base, reason="the lease-reaper tests need a local PostgreSQL")
    create_scratch_database(admin_dsn, WORKER_DB_NAME)
    url = base.set(database=WORKER_DB_NAME).render_as_string(hide_password=False)
    run_alembic(url, "upgrade", "head")
    yield url, to_asyncpg(base.set(database=WORKER_DB_NAME))
    drop_scratch_database(admin_dsn, WORKER_DB_NAME)


@pytest.fixture
def engine(worker_db: tuple[str, str]) -> Iterator[AsyncEngine]:
    # NullPool: connections must never cross the per-test event loops.
    made = create_async_engine(worker_db[0], poolclass=NullPool)
    yield made
    asyncio.run(made.dispose())


@pytest.fixture
def store(engine: AsyncEngine) -> PostgresJobStore:
    async def truncate() -> None:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE TABLE pr_review_jobs, periodic_task_runs CASCADE"))

    asyncio.run(truncate())
    return PostgresJobStore(engine)


@pytest.fixture
def dsn(worker_db: tuple[str, str]) -> str:
    return worker_db[1]


async def _write(engine: AsyncEngine, job_id: str, column: str) -> None:
    # `column` is one of two literals from this module, never caller input.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"UPDATE pr_review_jobs SET {column} = now() - interval '1 second' WHERE id = :id"
            ),
            {"id": uuid.UUID(job_id)},
        )


async def _expire_lease(engine: AsyncEngine, job_id: str) -> None:
    await _write(engine, job_id, "locked_until")


async def _make_retry_due(engine: AsyncEngine, job_id: str) -> None:
    await _write(engine, job_id, "next_attempt_at")


async def _db_now(engine: AsyncEngine) -> datetime:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT now()"))
        return result.scalar_one()


async def _get(store: PostgresJobStore, job_id: str):
    stored = await store.get(job_id)
    assert stored is not None
    return stored


# --- reap_expired -------------------------------------------------------------


async def test_an_expired_lease_returns_the_job_to_retrying(
    store: PostgresJobStore, engine: AsyncEngine
) -> None:
    job_id = await enqueue(store, "d-1")
    claimed = await store.claim("w-dead")
    assert claimed is not None and claimed.job_id == job_id
    await _expire_lease(engine, job_id)

    before = await _db_now(engine)
    assert await store.reap_expired() == 1
    after = await _db_now(engine)

    stored = await _get(store, job_id)
    assert stored.status is ReviewJobStatus.RETRYING
    assert stored.retry_count == 1
    assert stored.error_kind == "lease_expired"
    assert stored.finished_at is None
    # The reap SQL has no jitter: exactly the 60s base backoff on retry 0
    # (docs/PIPELINE_SPEC.md §4.3), computed from the database's now().
    assert stored.next_attempt_at is not None
    assert before + timedelta(seconds=60) <= stored.next_attempt_at
    assert stored.next_attempt_at <= after + timedelta(seconds=60)


async def test_reaping_exhausts_to_failed_with_the_retry_count_capped(
    store: PostgresJobStore, engine: AsyncEngine
) -> None:
    job_id = await enqueue(store, "d-1")

    for attempt in range(1, DEFAULT_MAX_RETRIES + 1):
        claimed = await store.claim(f"w-die-{attempt}")
        assert claimed is not None and claimed.job_id == job_id
        await _expire_lease(engine, job_id)
        assert await store.reap_expired() == 1
        stored = await _get(store, job_id)
        assert stored.status is ReviewJobStatus.RETRYING
        assert stored.retry_count == attempt
        await _make_retry_due(engine, job_id)

    claimed = await store.claim("w-die-final")
    assert claimed is not None and claimed.job_id == job_id
    await _expire_lease(engine, job_id)
    assert await store.reap_expired() == 1

    stored = await _get(store, job_id)
    assert stored.status is ReviewJobStatus.FAILED
    # LEAST(retry_count + 1, max_retries): capped, CHECK-safe.
    assert stored.retry_count == DEFAULT_MAX_RETRIES
    assert stored.error_kind == "lease_expired"
    assert stored.finished_at is not None
    assert await store.claim("w-next") is None


async def test_a_live_lease_is_left_alone(store: PostgresJobStore, engine: AsyncEngine) -> None:
    job_id = await enqueue(store, "d-1")
    claimed = await store.claim("w-alive")
    assert claimed is not None

    # locked_until is 600s out: the reaper must not touch it.
    assert await store.reap_expired() == 0
    stored = await _get(store, job_id)
    assert stored.status is ReviewJobStatus.PROCESSING
    empty_stats = JobStats(
        files_total=0,
        files_reviewable=0,
        hunks_total=0,
        chunks_total=0,
        skipped_counts=(),
    )
    assert await store.complete(job_id, empty_stats, worker_id="w-alive")


# --- reap_tick under the advisory lock ----------------------------------------


async def test_reap_tick_reaps_only_when_it_wins_the_lock(
    store: PostgresJobStore, engine: AsyncEngine, dsn: str
) -> None:
    job_id = await enqueue(store, "d-1")
    claimed = await store.claim("w-dead")
    assert claimed is not None
    await _expire_lease(engine, job_id)

    holder = await asyncpg.connect(dsn)
    try:
        # Another worker holds the advisory lock: this tick skips, and the
        # job is left for that worker's reap.
        assert await holder.fetchval("SELECT pg_try_advisory_lock($1)", TASK_KEYS["reaper"])
        assert await reap_tick(store, TASK_KEYS["reaper"], dsn) is None
        assert (await _get(store, job_id)).status is ReviewJobStatus.PROCESSING

        assert await holder.fetchval("SELECT pg_advisory_unlock($1)", TASK_KEYS["reaper"])
    finally:
        await holder.close()

    expiries = metrics.WORKER_REGISTRY.get_sample_value("lease_expiries_total") or 0.0
    assert await reap_tick(store, TASK_KEYS["reaper"], dsn, interval=30) == 1
    stored = await _get(store, job_id)
    assert stored.status is ReviewJobStatus.RETRYING
    assert stored.retry_count == 1
    assert stored.error_kind == "lease_expired"
    # The leader counts what it reaped and records its success for "Periodic task stale".
    assert metrics.WORKER_REGISTRY.get_sample_value("lease_expiries_total") == expiries + 1
    [run] = await store.periodic_runs()
    assert (run.task, run.interval_seconds) == ("reaper", 30.0)


async def test_a_skipped_tick_records_no_success(store: PostgresJobStore, dsn: str) -> None:
    holder = await asyncpg.connect(dsn)
    try:
        assert await holder.fetchval("SELECT pg_try_advisory_lock($1)", TASK_KEYS["reaper"])
        assert await reap_tick(store, TASK_KEYS["reaper"], dsn) is None
    finally:
        await holder.close()

    assert await store.periodic_runs() == ()


async def test_reap_tick_uses_the_injected_connection_factory(
    store: PostgresJobStore, dsn: str
) -> None:
    opened: list[str] = []

    async def connect(actual_dsn: str) -> asyncpg.Connection:
        opened.append(actual_dsn)
        return await asyncpg.connect(actual_dsn)

    assert await reap_tick(store, TASK_KEYS["reaper"], dsn, connect=connect) == 0
    assert opened == [dsn]
