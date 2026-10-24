"""ReviewCommentStore conformance suite: one suite against every adapter
(.agents/rules/backend.md § Tests).

Parametrized over MemoryReviewCommentStore and PostgresReviewCommentStore; each
is paired with the job store of the same kind, because a finding belongs to a
job and a pull request is found through its jobs.
"""

import asyncio
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from adapters.db.repository import PostgresJobStore
from adapters.db.review_comments import PostgresReviewCommentStore
from adapters.jobs.memory import MemoryJobStore
from adapters.memory.review_comments import MemoryReviewCommentStore
from domain.findings import NewPublishedFinding
from domain.models import (
    Category,
    DiffSide,
    Finding,
    FindingPosition,
    FindingSuggestion,
    Severity,
)
from domain.ports import JobRepository, ReviewCommentStore
from tests.db.support import (
    COMPOSE_URL,
    create_scratch_database,
    drop_scratch_database,
    require_postgres,
    run_alembic,
)
from tests.ports.test_job_repository import make_job

DB_NAME = "dmc268_review_comments_test"


@dataclass
class Env:
    jobs: JobRepository
    comments: ReviewCommentStore


def make_new(
    fingerprint: str,
    *,
    path: str = "app/service.py",
    line: int = 10,
    severity: Severity = Severity.MEDIUM,
    suggestion: FindingSuggestion | None = None,
    start_line: int | None = None,
    comment_id: int | None = 5001,
    review_id: int | None = 900,
) -> NewPublishedFinding:
    return NewPublishedFinding(
        fingerprint=fingerprint,
        finding=Finding(
            position=FindingPosition(path=path, line=line, side=DiffSide.NEW),
            severity=severity,
            category=Category.CORRECTNESS,
            message="The raw model message, which is not what gets stored.",
            suggestion=suggestion,
        ),
        body=f"Rendered comment <!-- {fingerprint} -->",
        start_line=start_line,
        external_comment_id=comment_id,
        external_review_id=review_id,
    )


def fp(char: str) -> str:
    return char * 64


@pytest.fixture
def memory_env() -> Env:
    jobs = MemoryJobStore()
    return Env(jobs=jobs, comments=MemoryReviewCommentStore(jobs))


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[AsyncEngine]:
    base = make_url(os.environ.get("DATABASE_URL") or COMPOSE_URL)
    admin_dsn = require_postgres(
        base, reason="the postgres param of the ReviewCommentStore suite needs a local PostgreSQL"
    )
    create_scratch_database(admin_dsn, DB_NAME)
    url = base.set(database=DB_NAME).render_as_string(hide_password=False)
    run_alembic(url, "upgrade", "head")
    # NullPool: connections must never cross the per-test event loops.
    engine = create_async_engine(url, poolclass=NullPool)
    yield engine
    asyncio.run(engine.dispose())
    drop_scratch_database(admin_dsn, DB_NAME)


@pytest.fixture
def postgres_env(pg_engine: AsyncEngine) -> Env:
    async def truncate() -> None:
        async with pg_engine.begin() as conn:
            await conn.execute(text("TRUNCATE TABLE pr_review_jobs CASCADE"))

    asyncio.run(truncate())
    return Env(jobs=PostgresJobStore(pg_engine), comments=PostgresReviewCommentStore(pg_engine))


@pytest.fixture(params=["memory", "postgres"])
def env(request: pytest.FixtureRequest) -> Env:
    return request.getfixturevalue(f"{request.param}_env")


async def enqueue(env: Env, **overrides: object) -> str:
    job_id = await env.jobs.enqueue(make_job(**overrides))
    assert job_id is not None
    return job_id


async def test_recorded_findings_read_back_most_severe_first(env: Env) -> None:
    job_id = await enqueue(env)
    suggestion = FindingSuggestion(before=("x = 1",), after=("x = 2", "y = 3"))
    await env.comments.record(
        job_id,
        [
            make_new(fp("a"), path="b.py", severity=Severity.LOW),
            make_new(fp("b"), path="b.py", line=3, severity=Severity.CRITICAL),
            make_new(
                fp("c"),
                path="a.py",
                line=8,
                severity=Severity.CRITICAL,
                suggestion=suggestion,
                start_line=7,
                comment_id=None,
            ),
        ],
    )

    found = await env.comments.for_job(job_id)

    assert [item.fingerprint for item in found] == [fp("c"), fp("b"), fp("a")]
    first = found[0]
    assert first.job_id == job_id
    assert (first.path, first.line, first.start_line, first.side) == ("a.py", 8, 7, DiffSide.NEW)
    assert (first.severity, first.category) == (Severity.CRITICAL, Category.CORRECTNESS)
    assert first.message == f"Rendered comment <!-- {fp('c')} -->"
    assert first.suggestion == suggestion
    assert (first.external_comment_id, first.external_review_id) == (None, 900)
    assert first.posted_at.tzinfo is not None
    uuid.UUID(first.id)
    assert found[1].suggestion is None
    assert found[1].external_comment_id == 5001


async def test_posted_fingerprints_cover_every_job_of_the_pull_request(env: Env) -> None:
    first = await enqueue(env, delivery_id="d-1")
    await env.comments.record(first, [make_new(fp("a"))])
    # A push supersedes the first job; what it posted is still on the pull request.
    second = await enqueue(env, delivery_id="d-2", head_sha="9" * 40)
    await env.comments.record(second, [make_new(fp("b"))])
    for delivery, overrides in (
        ("d-3", {"pr_number": 8}),
        ("d-4", {"repo_id": 1}),
        ("d-5", {"installation_id": 43}),
    ):
        other = await enqueue(env, delivery_id=delivery, **overrides)
        await env.comments.record(other, [make_new(fp("z"))])

    posted = await env.comments.posted_fingerprints(
        provider="github", installation_id=42, repo_id=751065667, pr_number=7
    )

    assert posted == frozenset({fp("a"), fp("b")})


async def test_a_repeated_fingerprint_stores_nothing_from_the_batch(env: Env) -> None:
    job_id = await enqueue(env)
    await env.comments.record(job_id, [make_new(fp("a"))])

    with pytest.raises(Exception):  # noqa: B017 - each store raises its own kind
        await env.comments.record(job_id, [make_new(fp("b")), make_new(fp("a"))])

    assert [item.fingerprint for item in await env.comments.for_job(job_id)] == [fp("a")]


@pytest.mark.parametrize("job_id", [str(uuid.uuid4()), "not-a-job"])
async def test_recording_for_an_unknown_job_raises(env: Env, job_id: str) -> None:
    with pytest.raises(Exception):  # noqa: B017 - each store raises its own kind
        await env.comments.record(job_id, [make_new(fp("a"))])


async def test_an_empty_batch_and_an_unknown_job_read_as_nothing(env: Env) -> None:
    job_id = await enqueue(env)
    await env.comments.record(job_id, [])

    assert await env.comments.for_job(job_id) == ()
    assert await env.comments.for_job(str(uuid.uuid4())) == ()
    assert await env.comments.for_job("not-a-job") == ()
