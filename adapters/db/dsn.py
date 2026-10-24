"""One DSN conversion: SQLAlchemy URL → plain ``postgresql://`` for asyncpg.

asyncpg cannot parse SQLAlchemy driver URLs (``postgresql+asyncpg://``), and
the worker needs bare asyncpg connections that sit outside the SQLAlchemy
pool — the LISTEN connection (docs/db_models_and_migrations.md §6) and the
reaper's per-tick advisory-lock connection. Two hand-rolled copies of the
fix would drift, so the worker and the DB test suites share this one.
"""

from sqlalchemy.engine import URL, make_url


def to_asyncpg(dsn: str | URL) -> str:
    """Render ``dsn`` (a SQLAlchemy URL or its string form) as the plain
    ``postgresql://`` DSN ``asyncpg.connect`` wants.

    The password is kept: the result goes straight to the driver and is
    never logged or echoed into an exception message.
    """
    url = make_url(dsn) if isinstance(dsn, str) else dsn
    return url.set(drivername="postgresql").render_as_string(hide_password=False)
