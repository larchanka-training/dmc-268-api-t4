"""GitHub's rate-limit headers into ``forge_rate_limit_remaining_ratio``
(docs/observability.md § Metrics): an httpx response hook on every App and installation
client, so no call site needs to remember it.

Only the ``core`` resource counts, the REST API every call here uses: search has a
budget of its own (30 a minute), whose ratio would say nothing about reviews.
"""

from collections.abc import Callable
from typing import Any

import httpx

from adapters.github.config import PROVIDER_NAME
from adapters.observability import metrics

_COUNTED_RESOURCES = frozenset({"core", ""})


async def observe_rate_limit(response: httpx.Response) -> None:
    headers = response.headers
    remaining, limit = headers.get("x-ratelimit-remaining"), headers.get("x-ratelimit-limit")
    if headers.get("x-ratelimit-resource", "") not in _COUNTED_RESOURCES:
        return
    if remaining is None or limit is None or not remaining.isdigit() or not limit.isdigit():
        return
    if int(limit) > 0:
        metrics.observe_rate_limit(PROVIDER_NAME, int(remaining), int(limit))


#: For ``httpx.AsyncClient(event_hooks=...)``.
RATE_LIMIT_HOOKS: dict[str, list[Callable[..., Any]]] = {"response": [observe_rate_limit]}
