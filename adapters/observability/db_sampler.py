"""The API's DB sampler (docs/observability.md § Application, "DB sampler").

Every 15 s it reads the queue's depth and sets the queue gauges. It runs in the API,
not the worker, so the gauges keep reporting when the worker dies, which is exactly
when a backlog starts. A failed sample removes the gauges instead of leaving the last
value: a stale number must never look current, and Postgres being down is "Target
down"'s alert. Failures are logged once when they start and once when they clear, like
the worker's retry loops (§ Levels).
"""

import asyncio
import contextlib
import logging

from adapters.observability.metrics import QUEUE_GAUGES
from domain.ports import QueueStats

SAMPLE_INTERVAL_SECONDS = 15.0


async def run_db_sampler(
    source: QueueStats,
    stop: asyncio.Event,
    *,
    interval: float = SAMPLE_INTERVAL_SECONDS,
    logger: logging.Logger | None = None,
) -> None:
    """Sample until ``stop`` is set; the first sample is taken at once."""
    log = logger if logger is not None else logging.getLogger(__name__)
    failing = False
    try:
        while not stop.is_set():
            try:
                QUEUE_GAUGES.snapshot = await source.queue_snapshot()
            except Exception as err:
                QUEUE_GAUGES.snapshot = None
                if not failing:
                    log.warning("metrics.sampler.failed", extra={"error_type": type(err).__name__})
                    failing = True
            else:
                if failing:
                    log.info("metrics.sampler.recovered")
                    failing = False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
    finally:
        # A stopped sampler reports nothing, like one whose last sample failed.
        QUEUE_GAUGES.snapshot = None
