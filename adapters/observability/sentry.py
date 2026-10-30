"""Sentry for the API and the worker: errors and traces.

The settings are docs/observability.md § Sampling and privacy. Each privacy
option below overrides an SDK default that would send what AGENTS.md hard rules
2 and 3 keep out: the local variables of failing frames (a raw webhook body, the
OAuth code), request bodies, and query strings (the OAuth callback's ``code`` and
``state``). The scrubbers apply the log rules to what is left: secrets redacted,
headers and ``extra`` fields allowlisted, and in production no exception message
(a provider's error body or a pydantic ``input_value=`` fragment would otherwise
become the issue title and travel on to email and Telegram).
"""

import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, cast
from urllib.parse import urlsplit

import sentry_sdk
import structlog
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.httpx import HttpxIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.transport import Transport
from sentry_sdk.types import Breadcrumb, BreadcrumbHint, Event, Hint, SamplingContext

from adapters.observability.config import SentrySettings
from adapters.observability.logging import ALLOWED_FIELDS
from domain.redaction import redact

logger = logging.getLogger(__name__)

_HEALTH_PATH = "/health"
#: The Sentry → Telegram relay's route (api/internal_alerts.py). Nothing from it may
#: reach Sentry: an event raised there would trigger the relay again, and its trace
#: would carry the Bot API call, whose URL holds the bot token.
RELAY_PATH = "/internal/alerts/sentry"

#: Request headers an event may carry; Sentry already drops cookies and
#: ``Authorization`` without PII, and the webhook signature has no business in it.
_ALLOWED_HEADERS = frozenset(
    {
        "accept",
        "content-length",
        "content-type",
        "host",
        "user-agent",
        "x-github-delivery",
        "x-github-event",
        "x-request-id",
    }
)

#: Context bound for logs (bind_context) that also becomes searchable tags.
_TAG_FIELDS = ("installation_id", "job_id", "delivery_id", "request_id", "error_kind")


def traces_sampler(context: SamplingContext) -> float:
    """Trace every transaction except the healthcheck, the SSE stream and the relay.

    The Docker healthcheck alone is 8,640 requests a day. An SSE stream
    (``GET /jobs/{job_id}/events``) stays open until its job ends while polling
    Postgres every second, so it would be one transaction with a span per poll.
    """
    scope = context.get("asgi_scope") or {}
    # Behind uvicorn --root-path /api the path carries the prefix (api/deps.py).
    path = str(scope.get("path", "")).removeprefix(str(scope.get("root_path", "")))
    if path in (_HEALTH_PATH, RELAY_PATH) or (
        path.startswith("/jobs/") and path.endswith("/events")
    ):
        return 0.0
    return 1.0


def scrub_event(*, production: bool) -> Callable[[Event, Hint], Event | None]:
    """``before_send`` and ``before_send_transaction``."""

    def scrub(event: Event, hint: Hint) -> Event | None:
        data = cast(dict[str, Any], event)
        if _from_relay(data):
            return None
        _scrub_request(data)
        _tag_context(data)
        if production:
            _replace_exception_values(data)
            logentry = data.get("logentry")
            if isinstance(logentry, dict):
                logentry.pop("formatted", None)
                logentry.pop("params", None)
            _allowlist_extra(data)
        return cast(Event, _redacted(data))

    return scrub


def scrub_breadcrumb(
    *, production: bool
) -> Callable[[Breadcrumb, BreadcrumbHint], Breadcrumb | None]:
    """``before_breadcrumb``: Sentry's logging integration reads stdlib records
    itself, bypassing the log processors; in production a logging breadcrumb
    keeps the logger, the level and the event name, never the values."""

    def scrub(crumb: Breadcrumb, hint: BreadcrumbHint) -> Breadcrumb | None:
        data: dict[str, Any] = crumb
        record = hint.get("log_record") if isinstance(hint, Mapping) else None
        if production and isinstance(record, logging.LogRecord):
            data["message"] = str(record.msg)
            data.pop("data", None)
        return cast(Breadcrumb, _redacted(data))

    return scrub


def init_sentry(settings: SentrySettings, *, transport: Transport | None = None) -> bool:
    """Initialise the SDK. Without a DSN it does nothing and returns False.
    ``transport`` is the test seam: tests capture envelopes instead of sending."""
    if not settings.enabled:
        logger.info("sentry.disabled")
        return False
    production = not settings.is_local
    sentry_sdk.init(
        dsn=settings.dsn,
        environment=settings.environment,
        release=settings.release,
        # No send_default_pii and no data_collection: both default to sending no
        # PII, and the SDK deprecates send_default_pii even when set to False.
        include_local_variables=False,
        max_request_body_size="never",
        # Auto-enabling off and our integrations listed: an LLM SDK integration
        # (OpenAI, Anthropic, ...) records prompts and replies by default, and this
        # way none can switch itself on when its library appears.
        auto_enabling_integrations=False,
        integrations=[
            StarletteIntegration(),
            FastApiIntegration(),
            HttpxIntegration(),
            SqlalchemyIntegration(),
        ],
        traces_sampler=traces_sampler,
        trace_propagation_targets=[],
        before_send=scrub_event(production=production),
        before_send_transaction=scrub_event(production=production),
        before_breadcrumb=scrub_breadcrumb(production=production),
        transport=transport,
    )
    logger.info("sentry.enabled")
    return True


# --- the review's trace -----------------------------------------------------------

#: The worker's root span per job (docs/observability.md § Review pipeline).
JOB_TRANSACTION = "review.job"


def current_trace_context() -> dict[str, str] | None:
    """The active trace as propagation headers, stored with a job at enqueue so the
    worker continues it; None when Sentry is off or no trace is active."""
    # A client without a DSN still counts as active (sentry_sdk.init(dsn=None)) and
    # still has a trace, but nothing it records goes anywhere.
    if not sentry_sdk.get_client().dsn:
        return None
    traceparent = sentry_sdk.get_traceparent()
    if not traceparent:
        return None
    context = {"sentry-trace": traceparent}
    baggage = sentry_sdk.get_baggage()
    if baggage:
        context["baggage"] = baggage
    return context


@contextmanager
def job_transaction(trace_context: Mapping[str, str] | None) -> Iterator[None]:
    """The ``review.job`` transaction, continuing the enqueuing request's trace when
    there is one, so webhook and review are one trace. A no-op with Sentry off."""
    transaction = sentry_sdk.continue_trace(
        dict(trace_context or {}), op="queue.task", name=JOB_TRANSACTION, source="task"
    )
    with sentry_sdk.start_transaction(transaction):
        yield


# --- helpers -------------------------------------------------------------------


def _from_relay(event: dict[str, Any]) -> bool:
    """Raised while handling the relay route: by its transaction name, or by the
    request URL when the event has no transaction."""
    if str(event.get("transaction", "")).endswith(RELAY_PATH):
        return True
    request = event.get("request")
    url = request.get("url") if isinstance(request, dict) else None
    return isinstance(url, str) and urlsplit(url).path.endswith(RELAY_PATH)


def _scrub_request(event: dict[str, Any]) -> None:
    request = event.get("request")
    if not isinstance(request, dict):
        return
    # On the OAuth callback the query string is the code and state.
    for key in ("query_string", "data", "cookies"):
        request.pop(key, None)
    headers = request.get("headers")
    if isinstance(headers, dict):
        request["headers"] = {k: v for k, v in headers.items() if k.lower() in _ALLOWED_HEADERS}


def _tag_context(event: dict[str, Any]) -> None:
    """Tags from the context the logs bind (and from the log record's fields), so
    errors can be filtered per tenant, job and delivery."""
    bound = structlog.contextvars.get_contextvars()
    raw_extra = event.get("extra")
    extra: dict[str, Any] = raw_extra if isinstance(raw_extra, dict) else {}
    tags = event.setdefault("tags", {})
    for key in _TAG_FIELDS:
        value = bound.get(key, extra.get(key))
        if value is not None:
            tags.setdefault(key, str(value))


def _replace_exception_values(event: dict[str, Any]) -> None:
    """Every exception in the chain keeps its type and frames; its message
    becomes the error_kind, the closed vocabulary that names what failed."""
    error_kind = str(event.get("tags", {}).get("error_kind", ""))
    exception = event.get("exception")
    values = exception.get("values") if isinstance(exception, dict) else None
    for value in values if isinstance(values, list) else []:
        if isinstance(value, dict):
            value["value"] = error_kind


def _allowlist_extra(event: dict[str, Any]) -> None:
    extra = event.get("extra")
    if isinstance(extra, dict):
        event["extra"] = {k: v for k, v in extra.items() if k in ALLOWED_FIELDS}


def _redacted(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)[0]
    if isinstance(value, dict):
        return {key: _redacted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    return value
