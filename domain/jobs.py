"""Constants and helpers shared by every review-job store.

Pure domain code (AGENTS.md hard rule 1): no imports from `adapters/`,
`api/`, `worker/` or any framework, and no I/O. The in-memory store and the
future PostgreSQL queue both import this module, so the numbers below live
exactly once and cannot drift between the two implementations.
"""

import random
from datetime import timedelta

#: At most this many jobs per installation may be PROCESSING at once —
#: fairness, not capacity (docs/BACKEND_ARCHITECTURE.md § Processes,
#: `max_in_flight_per_account`; docs/WORKFLOW_DESIGN.md §2 Step 4). Keyed on
#: installation_id: an MVP deviation until tenancy lands.
MAX_IN_FLIGHT_PER_INSTALLATION = 3

#: Retries allowed before a retryable failure becomes FAILED
#: (docs/PIPELINE_SPEC.md §4.2, `max_retries` default 3).
DEFAULT_MAX_RETRIES = 3

#: The claim lease: how long a worker may hold a job before the reaper
#: reclaims it — 10 minutes (docs/WORKFLOW_DESIGN.md §2 Step 4;
#: docs/PIPELINE_SPEC.md §3).
LEASE_SECONDS = 600

_BACKOFF_BASE = timedelta(seconds=60)
_BACKOFF_CAP = timedelta(minutes=15)
#: docs/PIPELINE_SPEC.md §4.3: jitter is ±20% of the base delay.
_JITTER_FRACTION = 0.2


def backoff_delay(retry_count: int, rng: random.Random) -> timedelta:
    """The retry delay after `retry_count` recorded retries.

    docs/PIPELINE_SPEC.md §4.3: ``min(60s * 2**retry_count, 15min)`` scaled
    by a uniform jitter factor in [0.8, 1.2], so a provider outage does not
    produce a synchronised retry storm when it ends. Pure: the rng is
    injected, so stores stay testable without sleeping or seeding globals.
    """
    if retry_count < 0:
        raise ValueError(f"retry_count must be >= 0, got {retry_count}")
    base = min(_BACKOFF_BASE * 2**retry_count, _BACKOFF_CAP)
    factor = 1.0 + rng.uniform(-_JITTER_FRACTION, _JITTER_FRACTION)
    return timedelta(seconds=base.total_seconds() * factor)


def clamp_for_storage(value: str, limit: int = 255) -> str:
    """Truncate a string so it fits its VARCHAR(255) column.

    The D5 metadata columns (`pr_title` can exceed the limit; see
    docs/PIPELINE_SPEC.md §7.2) store webhook-supplied strings that were
    never size-checked by the forge. Called by the code building a
    NewReviewJob — the dataclass itself stays validation-free for these
    untrusted values.
    """
    if len(value) <= limit:
        return value
    return value[:limit]
