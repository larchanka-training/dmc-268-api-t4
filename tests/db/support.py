"""Shared PostgreSQL helpers for the DB-backed test suites.

The Alembic round-trip suite (``tests/db/test_migrations.py``), the
JobRepository conformance suite (``tests/ports/test_job_repository.py``)
and the worker suites (``tests/worker/``) all run against a real
PostgreSQL server, not a fake (.agents/rules/backend.md § Tests), so they
share one way to resolve the server, require it (skip locally, fail in
CI), drive Alembic against a scratch database, and render a DSN asyncpg
can connect to.
"""

import asyncio
import os
from pathlib import Path

import asyncpg
import pytest
from alembic.config import Config
from sqlalchemy.engine import URL

from adapters.db.dsn import to_asyncpg
from alembic import command

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
COMPOSE_URL = "postgresql+asyncpg://dmc:dmc@localhost:5432/dmc268"


def run_alembic(url: str, verb: str, target: str) -> None:
    """Run one Alembic command (``upgrade``/``downgrade``) against ``url``.

    ``DATABASE_URL`` is swapped around the call because ``alembic/env.py``
    reads the server address from the environment.
    """
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


def asyncpg_dsn(url: URL) -> str:
    """Render a SQLAlchemy URL as the plain ``postgresql://`` DSN asyncpg
    wants. Delegates to ``adapters.db.dsn.to_asyncpg`` so the conversion
    has exactly one implementation."""
    return to_asyncpg(url)


async def _recreate_database(admin_dsn: str, name: str) -> None:
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        await conn.execute(f"CREATE DATABASE {name}")
    finally:
        await conn.close()


async def _drop_database_only(admin_dsn: str, name: str) -> None:
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    finally:
        await conn.close()


def create_scratch_database(admin_dsn: str, name: str) -> None:
    """Recreate an empty scratch database. ``name`` is always a suite
    constant, never caller input, so it cannot inject SQL."""
    asyncio.run(_recreate_database(admin_dsn, name))


def drop_scratch_database(admin_dsn: str, name: str) -> None:
    asyncio.run(_drop_database_only(admin_dsn, name))


async def server_reachable(admin_dsn: str) -> bool:
    """True when a PostgreSQL server answers on ``admin_dsn`` within 3 s."""
    try:
        conn = await asyncpg.connect(admin_dsn, timeout=3)
    except Exception:
        return False
    await conn.close()
    return True


def require_postgres(base: URL, *, reason: str) -> str:
    """Return the admin DSN of the server behind ``base``; skip or fail if down.

    Locally the suite skips with a loud reason (``docker compose up -d
    postgres``); in CI the server is required, so the run fails instead —
    a skip would silently drop the DB suites (.agents/rules/backend.md §
    Tests: queue behaviour is tested on real PostgreSQL, not a fake, and CI
    runs the upgrade/downgrade round trip).
    """
    admin_url = base.set(database="postgres")
    admin_dsn = asyncpg_dsn(admin_url)
    if asyncio.run(server_reachable(admin_dsn)):
        return admin_dsn
    where = admin_url.render_as_string(hide_password=True)
    if os.environ.get("CI") == "true":
        # (.agents/rules/backend.md § Tests) PostgreSQL is a requirement in
        # CI, not an option: never let a DB suite skip there.
        pytest.fail(f"PostgreSQL is required in CI but unreachable at {where}")
    pytest.skip(
        f"PostgreSQL is not reachable at {where} — {reason} "
        "(`docker compose up -d postgres`); CI always provides one"
    )
