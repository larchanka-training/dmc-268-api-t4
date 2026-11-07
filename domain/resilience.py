"""Retry delays and a circuit breaker for calls to external services.

Pure: time and randomness are injected, so adapters (GitHub, LLM) share one
policy and tests never sleep (docs/PIPELINE_SPEC.md §4.3, §4.4).
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

BreakerState = Literal["closed", "open", "half_open"]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """In-call retries: ``min(base * 2**n, cap)`` seconds with ±jitter."""

    max_attempts: int = 3
    base_seconds: float = 0.5
    cap_seconds: float = 8.0
    jitter_fraction: float = 0.2

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_seconds < 0 or self.cap_seconds < self.base_seconds:
            raise ValueError("need 0 <= base_seconds <= cap_seconds")

    def delay(
        self, attempt: int, rng: random.Random, retry_after: float | None = None
    ) -> float | None:
        """Seconds to wait before attempt ``attempt + 1`` (0-based), or None to give up.

        A server-sent Retry-After wins when it is larger than the backoff; one
        longer than ``cap_seconds`` is not waited out in-call (the job-level
        retry takes it over), so None is returned.
        """
        if attempt + 1 >= self.max_attempts:
            return None
        base = min(self.base_seconds * 2**attempt, self.cap_seconds)
        delay: float = base * (1.0 + rng.uniform(-self.jitter_fraction, self.jitter_fraction))
        if retry_after is not None:
            if retry_after > self.cap_seconds:
                return None
            delay = max(delay, retry_after)
        return delay


class CircuitBreaker:
    """Opens after ``failure_threshold`` consecutive failures, probes after a cool-down.

    closed -> open on the threshold; open -> half_open once ``reset_seconds``
    passed (one probe call is let through); a probe success closes it, a probe
    failure re-opens it. Single event loop only; no locking.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_seconds: float = 30.0,
        clock: Callable[[], float],
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self._threshold = failure_threshold
        self._reset = reset_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self._reset:
            return "half_open"
        return "open"

    def retry_in(self) -> float:
        """Seconds until a probe is allowed (0 when not open)."""
        if self._opened_at is None:
            return 0.0
        return max(0.0, self._reset - (self._clock() - self._opened_at))

    def allow(self) -> bool:
        """True if a call may go out now; claims the single probe slot when half-open."""
        state = self.state
        if state == "closed":
            return True
        if state == "half_open" and not self._probing:
            self._probing = True
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probing = False

    def record_failure(self) -> None:
        self._failures += 1
        was_probe = self._probing
        self._probing = False
        if was_probe or self._failures >= self._threshold:
            self._opened_at = self._clock()
