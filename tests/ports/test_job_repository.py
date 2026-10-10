"""JobRepository conformance suite: one suite against every adapter
(.agents/rules/backend.md § Tests; docs/BACKEND_ARCHITECTURE.md § Testing
strategy — queue behaviour runs on real PostgreSQL, not a fake).

Parametrized over MemoryJobStore and PostgresJobStore (a scratch database,
migrated by Alembic once per session). No test sleeps: the fake clock is a
step clock, and where the memory env advances it, the postgres env back-dates
the retry's ``next_attempt_at`` instead, because the SQL store's eligibility
window is the database's ``now()`` — the observable behaviour through the port
is identical.
"""

import asyncio
import os
import random
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from adapters.db.repository import NOTIFY_CHANNEL, PostgresJobStore
from adapters.jobs.memory import MemoryJobStore
from domain.jobs import DEFAULT_MAX_RETRIES, backoff_delay
from domain.ports import (
    ClaimedJob,
    JobRepository,
    JobStats,
    NewReviewJob,
    ReviewJobStatus,
)
from tests.db.support import COMPOSE_URL, asyncpg_dsn, require_postgres, run_alembic

HEAD_SHA = "a1b2c3d4e5" * 4
BASE_SHA = "f6e5d4c3b2" * 4
OTHER_SHA = "0123456789" * 4
PORTS_DB_NAME = "dmc268_ports_test"


class StepClock:
    """A domain.ports.Clock fake; advance it instead of sleeping.

    Starts at the real wall clock: the PostgreSQL store compares claim
    eligibility against the database's now(), so a fake pinned to the past
    would make every backoff-scheduled retry immediately due.
    """

    def __init__(self) -> None:
        self._now = datetime.now(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@dataclass
class StoreEnv:
    """One adapter behind the port, plus how its test clock moves."""

    store: JobRepository
    clock: StepClock
    make_retry_due: Callable[[str], Awaitable[None]]
    pg_dsn: str | None = None


def make_job(**overrides: object) -> NewReviewJob:
    values: dict[str, object] = {
        "provider": "github",
        "delivery_id": "d-1",
        "installation_id": 42,
        "repo_id": 751065667,
        "repo_full_name": "octocat/hello-world",
        "pr_number": 7,
        "head_sha": HEAD_SHA,
        "base_sha": BASE_SHA,
        "event_action": "opened",
        "pr_title": "Add a feature",
    }
    values.update(overrides)
    return NewReviewJob(**values)  # type: ignore[arg-type]


def make_stats() -> JobStats:
    return JobStats(
        files_total=2,
        files_reviewable=1,
        hunks_total=2,
        chunks_total=1,
        skipped_counts=(("lockfile", 1),),
    )


async def enqueue(store: JobRepository, job: NewReviewJob) -> str:
    job_id = await store.enqueue(job)
    assert job_id is not None
    return job_id


async def claim(store: JobRepository, worker_id: str = "w-1") -> ClaimedJob:
    claimed = await store.claim(worker_id)
    assert claimed is not None
    return claimed


# --- adapters under test ----------------------------------------------------


@pytest.fixture
def memory_env() -> StoreEnv:
    clock = StepClock()

    async def make_retry_due(job_id: str) -> None:
        # Past every backoff this suite produces (at most 4 min * 1.2 jitter).
        clock.advance(1200)

    return StoreEnv(
        store=MemoryJobStore(clock=clock, rng=random.Random(0)),
        clock=clock,
        make_retry_due=make_retry_due,
    )


async def _truncate(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE pr_review_steps, pr_review_jobs"))


async def _recreate_ports_database(admin_dsn: str) -> None:
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {PORTS_DB_NAME} WITH (FORCE)")
        await conn.execute(f"CREATE DATABASE {PORTS_DB_NAME}")
    finally:
        await conn.close()


async def _drop_ports_database(admin_dsn: str) -> None:
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {PORTS_DB_NAME} WITH (FORCE)")
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def pg_engine() -> AsyncIterator[AsyncEngine]:
    base = make_url(os.environ.get("DATABASE_URL") or COMPOSE_URL)
    admin_dsn = require_postgres(
        base,
        reason=(
            "the postgres param of the JobRepository conformance suite needs a local PostgreSQL"
        ),
    )
    asyncio.run(_recreate_ports_database(admin_dsn))
    url = base.set(database=PORTS_DB_NAME).render_as_string(hide_password=False)
    run_alembic(url, "upgrade", "head")
    # NullPool: connections must never cross the per-test event loops.
    engine = create_async_engine(url, poolclass=NullPool)
    yield engine
    asyncio.run(engine.dispose())
    asyncio.run(_drop_ports_database(admin_dsn))


@pytest.fixture
def postgres_env(pg_engine: AsyncEngine) -> StoreEnv:
    # Sync on purpose: fixture setup then runs outside the test's event loop,
    # so asyncio.run below is legal; the store's own methods run in-loop.
    asyncio.run(_truncate(pg_engine))

    async def make_retry_due(job_id: str) -> None:
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE pr_review_jobs"
                    " SET next_attempt_at = now() - interval '1 second'"
                    " WHERE id = :id"
                ),
                {"id": uuid.UUID(job_id)},
            )

    base = make_url(os.environ.get("DATABASE_URL") or COMPOSE_URL)
    clock = StepClock()
    return StoreEnv(
        store=PostgresJobStore(pg_engine, clock=clock, rng=random.Random(0)),
        clock=clock,
        make_retry_due=make_retry_due,
        pg_dsn=asyncpg_dsn(base.set(database=PORTS_DB_NAME)),
    )


@pytest.fixture(params=["memory", "postgres"])
def env(request: pytest.FixtureRequest) -> StoreEnv:
    return request.getfixturevalue(f"{request.param}_env")


# --- enqueue ----------------------------------------------------------------


async def test_enqueue_returns_an_id_and_the_job_is_queued(env: StoreEnv) -> None:
    job = make_job()
    job_id = await env.store.enqueue(job)
    assert job_id is not None

    stored = await env.store.get(job_id)
    assert stored is not None
    assert stored.status is ReviewJobStatus.QUEUED
    assert stored.payload == job
    assert stored.created_at.tzinfo is not None
    assert stored.retry_count == 0
    assert stored.finished_at is None
    assert stored.stats is None
    assert stored.error_kind is None
    # The schema's server_default now(): claimable immediately, never NULL.
    assert stored.next_attempt_at == stored.created_at


async def test_a_duplicate_delivery_returns_none_without_a_second_job(env: StoreEnv) -> None:
    first = await enqueue(env.store, make_job(delivery_id="d-1"))

    assert await env.store.enqueue(make_job(delivery_id="d-1", head_sha=OTHER_SHA)) is None

    # Dedup runs before supersession: the original is untouched, not
    # superseded by its own redelivery.
    stored = await env.store.get(first)
    assert stored is not None
    assert stored.status is ReviewJobStatus.QUEUED
    assert stored.finished_at is None


async def test_a_redelivered_duplicate_does_not_undo_the_supersession(env: StoreEnv) -> None:
    first = await enqueue(env.store, make_job(delivery_id="d-1"))
    assert await enqueue(env.store, make_job(delivery_id="d-2", head_sha=OTHER_SHA))

    assert await env.store.enqueue(make_job(delivery_id="d-1")) is None

    stored = await env.store.get(first)
    assert stored is not None
    assert stored.status is ReviewJobStatus.SUPERSEDED


async def test_the_same_delivery_id_under_another_provider_is_accepted(env: StoreEnv) -> None:
    assert await env.store.enqueue(make_job(delivery_id="d-1", provider="github")) is not None
    assert await env.store.enqueue(make_job(delivery_id="d-1", provider="gitlab")) is not None


async def test_a_null_delivery_id_never_deduplicates(env: StoreEnv) -> None:
    assert await env.store.enqueue(make_job(delivery_id=None, pr_number=7)) is not None
    assert await env.store.enqueue(make_job(delivery_id=None, pr_number=8)) is not None


# --- supersession -----------------------------------------------------------


async def test_enqueue_supersedes_the_prs_active_jobs_with_finished_at(env: StoreEnv) -> None:
    first = await enqueue(env.store, make_job(delivery_id="d-1"))
    second = await enqueue(env.store, make_job(delivery_id="d-2", head_sha=OTHER_SHA))

    superseded = await env.store.get(first)
    assert superseded is not None
    assert superseded.status is ReviewJobStatus.SUPERSEDED
    assert superseded.finished_at is not None

    active = await env.store.get(second)
    assert active is not None
    assert active.status is ReviewJobStatus.QUEUED
    assert active.finished_at is None


async def test_supersession_leaves_other_pull_requests_alone(env: StoreEnv) -> None:
    first = await enqueue(env.store, make_job(delivery_id="d-1", pr_number=7))
    assert await enqueue(env.store, make_job(delivery_id="d-2", pr_number=8))

    stored = await env.store.get(first)
    assert stored is not None
    assert stored.status is ReviewJobStatus.QUEUED
    assert stored.finished_at is None


# --- claim ------------------------------------------------------------------


async def test_claim_returns_the_oldest_eligible_job_first(env: StoreEnv) -> None:
    first = await enqueue(env.store, make_job(delivery_id="d-1", pr_number=7))
    second = await enqueue(env.store, make_job(delivery_id="d-2", pr_number=8))

    claimed = await claim(env.store, "w-1")
    assert claimed.job_id == first

    next_claimed = await claim(env.store, "w-2")
    assert next_claimed.job_id == second

    assert await env.store.claim("w-3") is None


async def test_claim_marks_the_job_processing_and_reports_the_attempt(env: StoreEnv) -> None:
    job_id = await enqueue(env.store, make_job())

    claimed = await claim(env.store, "w-1")

    assert claimed.job_id == job_id
    assert claimed.attempt == 0
    assert claimed.worker_id == "w-1"
    stored = await env.store.get(job_id)
    assert stored is not None
    assert stored.status is ReviewJobStatus.PROCESSING


async def test_claim_skips_a_future_attempt_until_it_is_due(env: StoreEnv) -> None:
    job_id = await enqueue(env.store, make_job())
    claimed = await claim(env.store)
    assert (
        await env.store.fail(
            job_id, "forge_unavailable", retryable=True, worker_id=claimed.worker_id
        )
        is ReviewJobStatus.RETRYING
    )

    stored = await env.store.get(job_id)
    assert stored is not None
    assert stored.next_attempt_at is not None
    assert stored.next_attempt_at > env.clock.now()

    assert await env.store.claim("w-2") is None

    await env.make_retry_due(job_id)
    reclaimed = await claim(env.store, "w-2")
    assert reclaimed.job_id == job_id
    assert reclaimed.attempt == 1


async def test_claim_is_fair_per_installation(env: StoreEnv) -> None:
    burst = [
        await enqueue(env.store, make_job(delivery_id=f"d-{index}", pr_number=7 + index))
        for index in range(4)
    ]
    other = await enqueue(
        env.store, make_job(delivery_id="d-other", installation_id=7, pr_number=1)
    )

    first = await claim(env.store, "w-1")
    second = await claim(env.store, "w-2")
    third = await claim(env.store, "w-3")
    assert first.job_id in burst
    assert second.job_id in burst
    assert third.job_id in burst

    # Installation 42 now runs MAX_IN_FLIGHT_PER_INSTALLATION jobs; its fourth
    # job waits while the other installation's job is claimed.
    fourth = await claim(env.store, "w-4")
    assert fourth.job_id == other

    # Only the blocked job remains, and it stays blocked.
    assert await env.store.claim("w-5") is None


# --- holder checks ----------------------------------------------------------


async def test_complete_by_a_non_holder_is_rejected_and_the_job_stays_processing(
    env: StoreEnv,
) -> None:
    job_id = await enqueue(env.store, make_job())
    await claim(env.store, "w-1")

    assert await env.store.complete(job_id, make_stats(), worker_id="w-2") is False

    stored = await env.store.get(job_id)
    assert stored is not None
    assert stored.status is ReviewJobStatus.PROCESSING


async def test_complete_by_the_holder_completes_and_roundtrips_stats_via_get(
    env: StoreEnv,
) -> None:
    job_id = await enqueue(env.store, make_job())
    claimed = await claim(env.store)
    stats = JobStats(
        files_total=3,
        files_reviewable=2,
        hunks_total=5,
        chunks_total=4,
        skipped_counts=(("lockfile", 2), ("generated", 1)),
    )

    assert await env.store.complete(job_id, stats, worker_id=claimed.worker_id) is True

    stored = await env.store.get(job_id)
    assert stored is not None
    assert stored.status is ReviewJobStatus.COMPLETED
    assert stored.stats == stats
    assert stored.finished_at is not None
    assert stored.error_kind is None


async def test_extend_lease_is_holder_checked(env: StoreEnv) -> None:
    job_id = await enqueue(env.store, make_job())
    await claim(env.store, "w-1")

    assert await env.store.extend_lease(job_id, worker_id="w-2") is False
    assert await env.store.extend_lease(job_id, worker_id="w-1") is True


async def test_get_returns_none_for_an_unknown_id(env: StoreEnv) -> None:
    assert await env.store.get("no-such-job") is None


# --- fail: retry, exhaustion, permanent -------------------------------------


async def test_fail_retryable_schedules_a_retry_in_the_seeded_backoff_window(
    env: StoreEnv,
) -> None:
    job_id = await enqueue(env.store, make_job())
    claimed = await claim(env.store)

    failed_at = env.clock.now()
    status = await env.store.fail(
        job_id, "forge_unavailable", retryable=True, worker_id=claimed.worker_id
    )

    assert status is ReviewJobStatus.RETRYING
    stored = await env.store.get(job_id)
    assert stored is not None
    assert stored.status is ReviewJobStatus.RETRYING
    assert stored.retry_count == 1
    assert stored.error_kind == "forge_unavailable"
    assert stored.finished_at is None
    # The store's rng is Random(0): a second Random(0) draws the same delay
    # (docs/PIPELINE_SPEC.md §4.3 — 60 s base with ±20% jitter on retry 0).
    expected = backoff_delay(0, random.Random(0))
    assert stored.next_attempt_at is not None
    assert stored.next_attempt_at - failed_at == expected
    assert timedelta(seconds=60 * 0.8) <= expected <= timedelta(seconds=60 * 1.2)


async def test_fail_permanent_fails_the_job_without_incrementing_retries(
    env: StoreEnv,
) -> None:
    job_id = await enqueue(env.store, make_job())
    claimed = await claim(env.store)

    status = await env.store.fail(
        job_id, "pr_not_found", retryable=False, worker_id=claimed.worker_id
    )

    assert status is ReviewJobStatus.FAILED
    stored = await env.store.get(job_id)
    assert stored is not None
    assert stored.status is ReviewJobStatus.FAILED
    assert stored.retry_count == 0
    assert stored.error_kind == "pr_not_found"
    assert stored.finished_at is not None


async def test_fail_retryable_after_max_retries_fails_with_retry_count_capped(
    env: StoreEnv,
) -> None:
    job_id = await enqueue(env.store, make_job())
    claimed = await claim(env.store)

    for expected_count in range(1, DEFAULT_MAX_RETRIES + 1):
        status = await env.store.fail(
            job_id, "forge_unavailable", retryable=True, worker_id=claimed.worker_id
        )
        assert status is ReviewJobStatus.RETRYING
        stored = await env.store.get(job_id)
        assert stored is not None
        assert stored.retry_count == expected_count

        await env.make_retry_due(job_id)
        claimed = await claim(env.store, "w-retry")
        assert claimed.attempt == expected_count

    status = await env.store.fail(
        job_id, "forge_unavailable", retryable=True, worker_id=claimed.worker_id
    )
    assert status is ReviewJobStatus.FAILED
    stored = await env.store.get(job_id)
    assert stored is not None
    assert stored.status is ReviewJobStatus.FAILED
    # Capped at max_retries (ck_pr_review_jobs_retry_bounds).
    assert stored.retry_count == DEFAULT_MAX_RETRIES
    assert stored.error_kind == "forge_unavailable"
    assert stored.finished_at is not None
    assert await env.store.claim("w-next") is None


async def test_fail_clears_the_lease_so_a_retry_is_reclaimable(env: StoreEnv) -> None:
    job_id = await enqueue(env.store, make_job())
    first = await claim(env.store, "w-1")

    status = await env.store.fail(
        job_id, "forge_unavailable", retryable=True, worker_id=first.worker_id
    )
    assert status is ReviewJobStatus.RETRYING

    # The lease is gone: the old holder cannot extend...
    assert await env.store.extend_lease(job_id, worker_id="w-1") is False
    await env.make_retry_due(job_id)
    # ...and another worker picks up the retry.
    reclaimed = await claim(env.store, "w-2")
    assert reclaimed.job_id == job_id
    assert reclaimed.attempt == 1


# --- terminal states --------------------------------------------------------


async def test_terminal_jobs_are_never_claimed_nor_transitioned(env: StoreEnv) -> None:
    superseded = await enqueue(env.store, make_job(delivery_id="d-1"))
    active = await enqueue(env.store, make_job(delivery_id="d-2", head_sha=OTHER_SHA))

    claimed = await claim(env.store)
    assert claimed.job_id == active
    assert await env.store.complete(active, make_stats(), worker_id=claimed.worker_id)

    # SUPERSEDED and COMPLETED are invisible to claim...
    assert await env.store.claim("w-2") is None
    # ...and reject every transition attempt.
    assert await env.store.complete(superseded, make_stats(), worker_id="w-2") is False
    assert (
        await env.store.fail(superseded, "forge_unavailable", retryable=True, worker_id="w-2")
        is None
    )
    assert await env.store.extend_lease(superseded, worker_id="w-2") is False


# --- postgres-only: concurrency and NOTIFY ----------------------------------


async def test_concurrent_claims_never_hand_out_the_same_job(postgres_env: StoreEnv) -> None:
    job_ids = []
    for index in range(5):
        job_id = await postgres_env.store.enqueue(
            make_job(delivery_id=f"d-{index}", pr_number=7 + index)
        )
        assert job_id is not None
        job_ids.append(job_id)

    first, second = await asyncio.gather(
        postgres_env.store.claim("w-1"),
        postgres_env.store.claim("w-2"),
    )
    assert first is not None
    assert second is not None
    assert first.job_id != second.job_id
    assert first.job_id in job_ids
    assert second.job_id in job_ids
    for claimed in (first, second):
        stored = await postgres_env.store.get(claimed.job_id)
        assert stored is not None
        assert stored.status is ReviewJobStatus.PROCESSING


async def test_enqueue_notifies_the_review_jobs_channel(postgres_env: StoreEnv) -> None:
    assert postgres_env.pg_dsn is not None
    listener = await asyncpg.connect(postgres_env.pg_dsn)
    try:
        loop = asyncio.get_running_loop()
        notified: asyncio.Future[tuple[str, str]] = loop.create_future()

        def on_notify(connection: asyncpg.Connection, pid: int, channel: str, payload: str) -> None:
            if not notified.done():
                notified.set_result((channel, payload))

        await listener.add_listener(NOTIFY_CHANNEL, on_notify)
        try:
            job_id = await postgres_env.store.enqueue(make_job())
            assert job_id is not None
            channel, payload = await asyncio.wait_for(notified, timeout=2.0)
        finally:
            await listener.remove_listener(NOTIFY_CHANNEL, on_notify)
        assert channel == NOTIFY_CHANNEL
        assert payload == ""
    finally:
        await listener.close()
