"""Runs a call to an external service with in-call retries and a circuit breaker.

The policy and the breaker live in domain/resilience.py; this module adds the
waiting. Shared by the GitHub and LLM adapters. Both are opt-in per adapter, so
a bare adapter still fails on the first error (docs/PIPELINE_SPEC.md §4.3).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from domain.resilience import CircuitBreaker, RetryPolicy

logger = logging.getLogger(__name__)

T = TypeVar("T")


class Resilience:
    def __init__(
        self,
        name: str,
        policy: RetryPolicy,
        breaker: CircuitBreaker,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._name = name
        self._policy = policy
        self._breaker = breaker
        self._sleep = sleep
        self._rng = rng if rng is not None else random.Random()

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    async def call(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        is_failure: Callable[[Exception], bool],
        can_retry: Callable[[Exception], bool],
        retry_after: Callable[[Exception], float | None] = lambda _: None,
        open_error: Callable[[float], Exception],
    ) -> T:
        """Run ``operation``.

        ``is_failure``: the service is unhealthy (feeds the breaker); any other
        exception means it answered, so the breaker counts a success.
        ``can_retry``: safe and useful to repeat. ``open_error``: what to raise
        while the breaker is open, given the seconds until the next probe.
        """
        attempt = 0
        while True:
            if not self._breaker.allow():
                logger.warning("resilience.circuit_open", extra={"provider": self._name})
                raise open_error(self._breaker.retry_in())
            try:
                result = await operation()
            except Exception as error:
                if not is_failure(error):
                    self._breaker.record_success()
                    raise
                self._breaker.record_failure()
                delay = (
                    self._policy.delay(attempt, self._rng, retry_after(error))
                    if can_retry(error)
                    else None
                )
                if delay is None:
                    raise
                logger.warning(
                    "resilience.retrying",
                    extra={"provider": self._name, "attempt": attempt + 1, "delay_seconds": delay},
                )
                await self._sleep(delay)
                attempt += 1
            else:
                self._breaker.record_success()
                return result


def default_resilience(name: str) -> Resilience:
    """The production policy: 3 attempts (0.5s, 1s backoff), 5 failures open the breaker for 30s."""
    return Resilience(
        name,
        RetryPolicy(),
        CircuitBreaker(failure_threshold=5, reset_seconds=30.0, clock=time.monotonic),
    )
