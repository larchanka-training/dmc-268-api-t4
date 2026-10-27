"""Job steps on real PostgreSQL: the worker records them, the SSE source reads them back."""

import asyncio
import os
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from adapters.db.job_events import PostgresJobEventsSource, PostgresStepRecorder
from adapters.db.repository import PostgresJobStore
from domain.errors import AllProvidersFailedError, ProviderUnavailableError
from domain.jobs import JobQuery, JobStep, StepStatus
from domain.ports import ReviewJobStatus
from tests.db.support import (
    COMPOSE_URL,
    create_scratch_database,
    drop_scratch_database,
    require_postgres,
    run_alembic,
)
from tests.worker.test_process import GOOD_DIFF, FakeGitProvider, make_context, make_payload
from tests.worker.test_review_stage import FakeGateway, FakePublisher
from worker.process import ReviewStage, process_claimed_job

DB_NAME = "dmc268_job_events_test"


@pytest.fixture(scope="module")
def pg_engine() -> Iterator[AsyncEngine]:
    base = make_url(os.environ.get("DATABASE_URL") or COMPOSE_URL)
    admin_dsn = require_postgres(base, reason="job step recording needs a local PostgreSQL")
    create_scratch_database(admin_dsn, DB_NAME)
    url = base.set(database=DB_NAME).render_as_string(hide_password=False)
    run_alembic(url, "upgrade", "head")
    # NullPool: connections must never cross the per-test event loops.
    engine = create_async_engine(url, poolclass=NullPool)
    yield engine
    asyncio.run(engine.dispose())
    drop_scratch_database(admin_dsn, DB_NAME)


@pytest.fixture
def engine(pg_engine: AsyncEngine) -> AsyncEngine:
    async def truncate() -> None:
        async with pg_engine.begin() as conn:
            await conn.execute(text("TRUNCATE TABLE pr_review_jobs CASCADE"))

    asyncio.run(truncate())
    return pg_engine


async def run_review(engine: AsyncEngine, gateway: FakeGateway) -> str:
    store = PostgresJobStore(engine)
    job_id = await store.enqueue(make_payload())
    assert job_id is not None
    claimed = await store.claim("w-1")
    assert claimed is not None
    await process_claimed_job(
        claimed,
        FakeGitProvider(make_context(GOOD_DIFF)),
        store,
        review=ReviewStage(gateway=gateway, publisher=FakePublisher()),
        steps=PostgresStepRecorder(engine),
    )
    return job_id


async def test_a_completed_review_reads_back_as_steps_then_its_status(engine: AsyncEngine) -> None:
    job_id = await run_review(engine, FakeGateway())
    source = PostgresJobEventsSource(engine)

    snapshot = await source.snapshot(job_id)

    assert snapshot is not None
    assert snapshot.installation_id == make_payload().installation_id
    assert snapshot.status is ReviewJobStatus.COMPLETED
    assert [(e.step, e.step_status) for e in snapshot.steps] == [
        (JobStep.CLAIMED, StepStatus.OK),
        (JobStep.FETCH_DIFF, StepStatus.OK),
        (JobStep.LLM_CALL, StepStatus.OK),
        (JobStep.POST_FEEDBACK, StepStatus.OK),
    ]
    # Four steps, each started and finished, then COMPLETED.
    assert snapshot.last_seq == 9
    events = await source.next_events(job_id, after_seq=7, timeout_seconds=0)
    assert [(e.seq, e.status, e.step) for e in events] == [
        (8, ReviewJobStatus.PROCESSING, JobStep.POST_FEEDBACK),
        (9, ReviewJobStatus.COMPLETED, None),
    ]


async def test_a_retried_review_shows_the_failed_step_and_retrying(engine: AsyncEngine) -> None:
    error = AllProvidersFailedError([ProviderUnavailableError("p", "down")])
    job_id = await run_review(engine, FakeGateway(error=error))

    events = await PostgresJobEventsSource(engine).next_events(job_id, 0, timeout_seconds=0)

    assert [(e.step, e.step_status, e.error_kind) for e in events[-2:]] == [
        (JobStep.LLM_CALL, StepStatus.FAILED, "provider_unavailable"),
        (None, None, None),
    ]
    assert events[-1].status is ReviewJobStatus.RETRYING


async def test_a_worker_without_the_lease_writes_no_steps(engine: AsyncEngine) -> None:
    store = PostgresJobStore(engine)
    job_id = await store.enqueue(make_payload())
    assert job_id is not None
    await store.claim("w-1")
    recorder = PostgresStepRecorder(engine)

    await recorder.start(job_id, 0, JobStep.CLAIMED, worker_id="w-2")

    snapshot = await PostgresJobEventsSource(engine).snapshot(job_id)
    assert snapshot is not None and snapshot.steps == ()


async def test_build_context_metrics_round_trip_through_the_step_row(
    engine: AsyncEngine,
) -> None:
    store = PostgresJobStore(engine)
    job_id = await store.enqueue(make_payload())
    assert job_id is not None
    claimed = await store.claim("w-1")
    assert claimed is not None and claimed.job_id == job_id

    recorder = PostgresStepRecorder(engine)
    await recorder.start(job_id, 0, JobStep.BUILD_CONTEXT, worker_id="w-1")
    await recorder.finish(
        job_id,
        0,
        JobStep.BUILD_CONTEXT,
        StepStatus.OK,
        worker_id="w-1",
        metrics={
            "context_files": 2,
            "context_fetches": 3,
            "tier1_symbols": 4,
            "tier2_symbols": 1,
            "fallback_files": 0,
            "parse_errors": 0,
            "context_tokens": 812,
            "context_cut": "none",
        },
    )

    # Metrics live on the step row, not in the SSE stream: read the table.
    from sqlalchemy import text as sql_text

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sql_text(
                    "SELECT metrics FROM pr_review_steps"
                    " WHERE job_id = :id AND step = 'BUILD_CONTEXT'"
                ),
                {"id": job_id},
            )
        ).first()
    assert row is not None and row.metrics is not None
    assert row.metrics["context_tokens"] == 812
    assert row.metrics["context_cut"] == "none"


async def test_a_waiting_stream_sees_a_step_recorded_meanwhile(engine: AsyncEngine) -> None:
    store = PostgresJobStore(engine)
    job_id = await store.enqueue(make_payload())
    assert job_id is not None
    await store.claim("w-1")
    source = PostgresJobEventsSource(engine, poll_seconds=0.05)

    waiting = asyncio.create_task(source.next_events(job_id, 0, timeout_seconds=5))
    await asyncio.sleep(0.1)
    await PostgresStepRecorder(engine).start(job_id, 0, JobStep.CLAIMED, worker_id="w-1")

    [event] = await waiting
    assert (event.seq, event.step, event.step_status) == (1, JobStep.CLAIMED, StepStatus.RUNNING)


async def test_an_idle_stream_times_out_empty_and_an_unknown_job_is_none(
    engine: AsyncEngine,
) -> None:
    store = PostgresJobStore(engine)
    job_id = await store.enqueue(make_payload())
    assert job_id is not None
    source = PostgresJobEventsSource(engine, poll_seconds=0.01)

    assert await source.next_events(job_id, 0, timeout_seconds=0.05) == ()
    assert await source.snapshot("0" * 32) is None
    assert await source.snapshot("not-a-job") is None


async def test_the_store_lists_jobs_with_filters_and_reads_steps(engine: AsyncEngine) -> None:
    store = PostgresJobStore(engine)
    job_id = await run_review(engine, FakeGateway())
    for number in (1, 2, 3):
        await store.enqueue(
            make_payload(delivery_id=f"d-{number}0", pr_number=number, repo_id=number)
        )
    await store.enqueue(make_payload(delivery_id="d-x", installation_id=999, repo_id=9))

    everything = await store.list_page(JobQuery(installation_ids=frozenset({42}), limit=2))
    rest = await store.list_page(
        JobQuery(installation_ids=frozenset({42}), limit=2, after=everything.next_after)
    )
    found = await store.list_page(JobQuery(installation_ids=frozenset({42}), text="#2"))
    titled = await store.list_page(JobQuery(installation_ids=frozenset({42}), text="100%"))
    done = await store.list_page(
        JobQuery(installation_ids=frozenset({42}), statuses=frozenset({ReviewJobStatus.COMPLETED}))
    )

    assert len(everything.items) == 2 and len(rest.items) == 2 and rest.next_after is None
    listed = [job.job_id for job in everything.items + rest.items]
    assert len(set(listed)) == 4 and job_id in listed
    assert [job.payload.pr_number for job in found.items] == [2]
    assert titled.items == ()
    [completed] = done.items
    assert completed.job_id == job_id and completed.started_at is not None
    assert [step.step for step in await store.steps(completed.job_id)] == [
        JobStep.CLAIMED,
        JobStep.FETCH_DIFF,
        JobStep.LLM_CALL,
        JobStep.POST_FEEDBACK,
    ]
