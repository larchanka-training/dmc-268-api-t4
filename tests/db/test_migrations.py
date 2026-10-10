"""Alembic round-trip tests against real PostgreSQL (CI runs the same loop:
upgrade head, downgrade base, upgrade again — docs/BACKEND_ARCHITECTURE.md §
Testing strategy). Locally `docker compose up -d postgres` provides the server;
the module skips loudly when neither DATABASE_URL nor the compose default is
reachable.
"""

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
COMPOSE_URL = "postgresql+asyncpg://dmc:dmc@localhost:5432/dmc268"
TEST_DB_NAME = "dmc268_test"

JOB_INDEXES = (
    "ix_pr_review_jobs_claim",
    "ix_pr_review_jobs_active_pr",
    "ix_pr_review_jobs_reaper",
    "ix_pr_review_jobs_inflight",
    "ix_pr_review_jobs_pr_history",
)

TABLES_SQL = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
JOB_INDEXES_SQL = (
    "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'pr_review_jobs'"
)

INSERT_JOB_SQL = """
    INSERT INTO pr_review_jobs
        (delivery_id, provider, installation_id, repo_id, repo_full_name,
         pr_number, head_sha, base_sha, event_action, pr_title, status)
    VALUES
        (:delivery_id, 'github', 101, 42, 'acme/payments', 7,
         :sha, :sha, 'opened', 'Add payments', :status)
"""


def _job_params(delivery_id: str, status: str = "QUEUED") -> dict[str, object]:
    return {
        "delivery_id": delivery_id,
        "sha": "a" * 40,
        "status": status,
    }


def _run_alembic(url: str, verb: str, target: str) -> None:
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        cfg = Config(str(ALEMBIC_INI))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        getattr(command, verb)(cfg, target)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


async def _fetch_all(url: str, statement: str, params: dict[str, object] | None = None):
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(statement), params or {})
            return list(result)
    finally:
        await engine.dispose()


async def _execute(url: str, statement: str, params: dict[str, object]) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(statement), params)
    finally:
        await engine.dispose()


async def _expect_integrity_error(url: str, statement: str, params: dict[str, object]) -> None:
    with pytest.raises(IntegrityError):
        await _execute(url, statement, params)


async def _table_names(url: str) -> set[str]:
    return {row[0] for row in await _fetch_all(url, TABLES_SQL)}


def _asyncpg_dsn(url) -> str:
    # asyncpg wants a plain postgresql:// DSN, without SQLAlchemy's +asyncpg.
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


async def _server_reachable(admin_dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(admin_dsn, timeout=3)
    except Exception:
        return False
    await conn.close()
    return True


async def _recreate_test_database(admin_dsn: str) -> None:
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")
        await conn.execute(f"CREATE DATABASE {TEST_DB_NAME}")
    finally:
        await conn.close()


async def _drop_test_database(admin_dsn: str) -> None:
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)")
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def migrated_url():
    base = make_url(os.environ.get("DATABASE_URL") or COMPOSE_URL)
    admin_dsn = _asyncpg_dsn(base.set(database="postgres"))
    if not asyncio.run(_server_reachable(admin_dsn)):
        pytest.skip(
            "PostgreSQL is not reachable at "
            f"{base.set(database='postgres').render_as_string(hide_password=True)} "
            "— the Alembic round-trip tests need a local PostgreSQL "
            "(`docker compose up -d postgres`); CI always provides one"
        )
    asyncio.run(_recreate_test_database(admin_dsn))
    url = base.set(database=TEST_DB_NAME).render_as_string(hide_password=False)
    _run_alembic(url, "upgrade", "head")
    yield url
    asyncio.run(_drop_test_database(admin_dsn))


def test_upgrade_head_creates_tables_indexes_and_constraints(migrated_url: str) -> None:
    tables = asyncio.run(_table_names(migrated_url))
    assert "pr_review_jobs" in tables
    assert "pr_review_steps" in tables

    index_names = {row[0] for row in asyncio.run(_fetch_all(migrated_url, JOB_INDEXES_SQL))}
    for name in JOB_INDEXES:
        assert name in index_names

    asyncio.run(_execute(migrated_url, INSERT_JOB_SQL, _job_params("dup-1")))
    asyncio.run(_expect_integrity_error(migrated_url, INSERT_JOB_SQL, _job_params("dup-1")))
    asyncio.run(
        _expect_integrity_error(
            migrated_url, INSERT_JOB_SQL, _job_params("bad-status", status="WEIRD")
        )
    )


def test_updated_at_trigger_overwrites_manual_writes(migrated_url: str) -> None:
    asyncio.run(_execute(migrated_url, INSERT_JOB_SQL, _job_params("trigger-1")))
    # Attempt to write an ancient updated_at: the BEFORE UPDATE trigger must
    # overwrite it with now() — no sleep needed for determinism.
    asyncio.run(
        _execute(
            migrated_url,
            "UPDATE pr_review_jobs SET updated_at = :old WHERE delivery_id = :delivery",
            {"old": datetime(2020, 1, 1, tzinfo=UTC), "delivery": "trigger-1"},
        )
    )
    rows = asyncio.run(
        _fetch_all(
            migrated_url,
            "SELECT updated_at FROM pr_review_jobs WHERE delivery_id = :delivery",
            {"delivery": "trigger-1"},
        )
    )
    assert rows[0][0] > datetime(2020, 1, 2, tzinfo=UTC)


def test_downgrade_base_drops_tables(migrated_url: str) -> None:
    _run_alembic(migrated_url, "downgrade", "base")
    tables = asyncio.run(_table_names(migrated_url))
    assert "pr_review_jobs" not in tables
    assert "pr_review_steps" not in tables


def test_reupgrade_head_is_repeatable(migrated_url: str) -> None:
    _run_alembic(migrated_url, "upgrade", "head")
    tables = asyncio.run(_table_names(migrated_url))
    assert "pr_review_jobs" in tables
    assert "pr_review_steps" in tables
    versions = asyncio.run(_fetch_all(migrated_url, "SELECT version_num FROM alembic_version"))
    assert versions[0][0] == "0001"
