"""Worker process settings, read once at startup.

The style of ``adapters/github/config.py`` and ``adapters/db/config.py``: a
frozen slots dataclass built by ``from_env()``, where every bad value is an
error naming its variable. The database settings are composed rather than
re-read — ``WorkerSettings.from_env`` builds a ``DBSettings`` internally —
so ``DATABASE_URL`` keeps exactly one validation path. The DSN it carries
is a secret; ``DBSettings`` suppresses it from reprs, so it never appears
in a ``WorkerSettings`` repr either.
"""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass

from adapters.db.config import DBSettings
from domain.errors import WorkerConfigurationError

#: docs/BACKEND_ARCHITECTURE.md § Processes: the default is 5, bounded by
#: the LLM provider's and the forge's concurrency limits, not by CPU.
DEFAULT_WORKER_CONCURRENCY = 5
#: docs/WORKFLOW_DESIGN.md §1: the poll fallback cadence — a missed NOTIFY
#: costs at most this much latency, never correctness.
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
#: docs/WORKFLOW_DESIGN.md §6: the reaper recovers jobs whose worker died;
#: 30s keeps a dead worker's jobs stuck for at most about half a minute.
DEFAULT_REAPER_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    db: DBSettings
    worker_concurrency: int = DEFAULT_WORKER_CONCURRENCY
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    reaper_interval_seconds: float = DEFAULT_REAPER_INTERVAL_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "WorkerSettings":
        source = os.environ if env is None else env
        return cls(
            db=DBSettings.from_env(source),
            worker_concurrency=_positive_int(
                source, "WORKER_CONCURRENCY", DEFAULT_WORKER_CONCURRENCY
            ),
            poll_interval_seconds=_positive_float(
                source, "POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS
            ),
            reaper_interval_seconds=_positive_float(
                source, "REAPER_INTERVAL_SECONDS", DEFAULT_REAPER_INTERVAL_SECONDS
            ),
        )


def _positive_int(source: Mapping[str, str], name: str, default: int) -> int:
    raw = source.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise WorkerConfigurationError(f"{name} must be an integer, got '{raw}'") from None
    if value <= 0:
        raise WorkerConfigurationError(f"{name} must be > 0, got {value}")
    return value


def _positive_float(source: Mapping[str, str], name: str, default: float) -> float:
    raw = source.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise WorkerConfigurationError(f"{name} must be a number, got '{raw}'") from None
    # NaN and infinity parse but are unusable as intervals (and NaN even
    # slips past the ``<= 0`` check, since every comparison with it is False).
    if not math.isfinite(value):
        raise WorkerConfigurationError(f"{name} must be a finite number, got '{raw}'")
    if value <= 0:
        raise WorkerConfigurationError(f"{name} must be > 0, got {value}")
    return value
