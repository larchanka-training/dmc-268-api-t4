from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from adapters.db.config import DBSettings


def build_engine(settings: DBSettings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.pool_size,
        pool_pre_ping=True,
        echo=False,
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False is not optional under asyncio (docs/db_models_
    # and_migrations.md §2): with the default, attribute access after commit()
    # refreshes implicitly and raises MissingGreenlet. Each process (API,
    # worker) builds its own engine; there is no module-level one.
    return async_sessionmaker(engine, expire_on_commit=False)
