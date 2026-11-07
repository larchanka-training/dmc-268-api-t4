"""What counts as a failure, and a retry, for GitHub calls (see adapters/resilience.py)."""

from __future__ import annotations

from adapters.github.config import PROVIDER_NAME
from domain.errors import ForgeRateLimitedError, ForgeUnavailableError


def is_forge_failure(error: Exception) -> bool:
    """GitHub is unhealthy (5xx, timeout, network). A 404, 403 or 422 means it answered.

    A throttle (429) is not an outage: it is per installation, and one busy tenant must
    not open the breaker shared by every other one. It is still retried after its wait.
    """
    return isinstance(error, ForgeUnavailableError) and not isinstance(error, ForgeRateLimitedError)


def forge_retry_after(error: Exception) -> float | None:
    if isinstance(error, ForgeRateLimitedError) and error.retry_after_seconds is not None:
        return float(error.retry_after_seconds)
    return None


def forge_open_error(retry_in: float) -> Exception:
    return ForgeUnavailableError(PROVIDER_NAME, f"circuit open, next probe in {retry_in:.0f}s")
