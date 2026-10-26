"""Observability settings, read once at startup.

The style of ``adapters/db/config.py``: a frozen slots dataclass built by
``from_env()``. Nothing here is required: an unset ``SENTRY_DSN`` turns Sentry
off, so local runs and tests send nothing (AGENTS.md hard rule 4); production
gets its DSN from deploy.yml, which refuses to deploy without one.
"""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from domain.errors import ObservabilityConfigurationError

#: docs/observability.md § Configuration and secrets: ENV is ``local`` unless
#: deploy.yml writes ``production``.
DEFAULT_ENV = "local"
DEFAULT_LOG_LEVEL = "INFO"
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def read_env(source: Mapping[str, str]) -> str:
    """``ENV``, or ``local`` when unset: the one place its default lives."""
    return source.get("ENV", "").strip() or DEFAULT_ENV


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    environment: str = DEFAULT_ENV
    level: str = DEFAULT_LOG_LEVEL
    #: Unset means stdout only, so a laptop or CI runner never needs a writable /logs.
    log_dir: str | None = None
    release: str | None = None

    @property
    def is_local(self) -> bool:
        return self.environment == DEFAULT_ENV

    @property
    def level_number(self) -> int:
        return logging.getLevelNamesMapping()[self.level]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LoggingSettings":
        source = os.environ if env is None else env

        def text(name: str) -> str:
            return source.get(name, "").strip()

        level = text("LOG_LEVEL").upper() or DEFAULT_LOG_LEVEL
        if level not in _LOG_LEVELS:
            raise ObservabilityConfigurationError(
                f"LOG_LEVEL must be one of {', '.join(_LOG_LEVELS)}, got '{text('LOG_LEVEL')}'"
            )
        return cls(
            environment=read_env(source),
            level=level,
            log_dir=text("LOG_DIR") or None,
            release=text("IMAGE_TAG") or None,
        )


@dataclass(frozen=True, slots=True)
class SentrySettings:
    # Not a credential (a DSN only lets you send events), but a leaked one lets
    # anyone file fake issues and use up the quota, so it stays out of reprs.
    dsn: str = field(default="", repr=False)
    environment: str = DEFAULT_ENV
    release: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.dsn)

    @property
    def is_local(self) -> bool:
        return self.environment == DEFAULT_ENV

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SentrySettings":
        source = os.environ if env is None else env

        def text(name: str) -> str:
            return source.get(name, "").strip()

        return cls(
            dsn=text("SENTRY_DSN"),
            environment=read_env(source),
            # The commit sha deploy.yml also names the Sentry release with, so
            # each error lines up with its release and commits.
            release=text("IMAGE_TAG") or None,
        )
