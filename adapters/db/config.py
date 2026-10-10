import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from domain.errors import DBConfigurationError

_URL_PREFIX = "postgresql+asyncpg://"
# docs/db_models_and_migrations.md §2: the pool follows worker concurrency
# (worker_concurrency = 5, docs/BACKEND_ARCHITECTURE.md § Worker), plus 2.
DEFAULT_POOL_SIZE = 7


@dataclass(frozen=True, slots=True)
class DBSettings:
    # Carries credentials, so it never appears in a repr.
    database_url: str = field(repr=False)
    pool_size: int = DEFAULT_POOL_SIZE

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DBSettings":
        source = os.environ if env is None else env
        url = source.get("DATABASE_URL", "").strip()
        if not url:
            raise DBConfigurationError("missing required environment variable(s): DATABASE_URL")
        # The scheme is not echoed back: the URL carries credentials.
        if not url.startswith(_URL_PREFIX):
            raise DBConfigurationError(f"DATABASE_URL must start with '{_URL_PREFIX}'")

        pool_size = DEFAULT_POOL_SIZE
        raw_pool = source.get("POOL_SIZE", "").strip()
        if raw_pool:
            try:
                pool_size = int(raw_pool)
            except ValueError:
                raise DBConfigurationError(
                    f"POOL_SIZE must be an integer, got '{raw_pool}'"
                ) from None
            if pool_size <= 0:
                raise DBConfigurationError(f"POOL_SIZE must be > 0, got {pool_size}")

        return cls(database_url=url, pool_size=pool_size)
