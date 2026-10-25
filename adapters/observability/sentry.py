"""Sentry for the API and the worker: errors and traces.

The settings are docs/observability.md § Sampling and privacy. Each privacy
option below overrides an SDK default that would send what AGENTS.md hard rules
2 and 3 keep out: the local variables of failing frames (a raw webhook body, the
OAuth code), request bodies, and query strings (the OAuth callback's ``code`` and
``state``). ``send_default_pii`` is already off by default; it is set anyway so
the choice is visible.
"""

import logging

import sentry_sdk
from sentry_sdk.types import Event, Hint, SamplingContext

from adapters.observability.config import SentrySettings

logger = logging.getLogger(__name__)

_HEALTH_PATH = "/health"


def traces_sampler(context: SamplingContext) -> float:
    """Trace every transaction except the healthcheck and the SSE stream.

    The Docker healthcheck alone is 8,640 requests a day. An SSE stream
    (``GET /jobs/{job_id}/events``) stays open until its job ends while polling
    Postgres every second, so it would be one transaction with a span per poll.
    """
    scope = context.get("asgi_scope") or {}
    # Behind uvicorn --root-path /api the path carries the prefix (api/deps.py).
    path = str(scope.get("path", "")).removeprefix(str(scope.get("root_path", "")))
    if path == _HEALTH_PATH or (path.startswith("/jobs/") and path.endswith("/events")):
        return 0.0
    return 1.0


def drop_query_string(event: Event, hint: Hint) -> Event | None:
    """Remove the request's query string: on the OAuth callback it is the code and state."""
    request = event.get("request")
    if request is not None:
        request.pop("query_string", None)
    return event


def init_sentry(settings: SentrySettings) -> bool:
    """Initialise the SDK. Without a DSN it does nothing and returns False."""
    if not settings.enabled:
        logger.info("sentry.disabled")
        return False
    sentry_sdk.init(
        dsn=settings.dsn,
        environment=settings.environment,
        release=settings.release,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        traces_sampler=traces_sampler,
        before_send=drop_query_string,
        before_send_transaction=drop_query_string,
    )
    logger.info("sentry.enabled")
    return True
