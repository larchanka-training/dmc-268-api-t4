"""The Sentry → Telegram alert relay: ``POST /internal/alerts/sentry``.

docs/observability.md § Alerts, "The relay". Sentry's internal integration posts a
signed delivery for each issue alert (resource ``event_alert``); the relay checks the
signature, drops stale, repeated and excess alerts, and forwards a short message.

Two rules keep it from feeding itself: a failure is logged at ``warning`` and counted
(``alert_relay_failures_total``, alerted from Grafana), never logged at ``error``; and
``before_send`` drops any Sentry event raised inside this route. Server to server, like
the webhooks: no session, CSRF-exempt (api/deps.py), the signature is the
authentication. Forwarding runs after the response, because Sentry gives a delivery
one second.
"""

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter, BackgroundTasks, Request, Response

from adapters.observability import metrics
from adapters.observability.sentry import RELAY_PATH
from api.deps import ApiError
from domain.alerts import Alert, RelayGate
from domain.ports import AlertNotifier, Clock
from domain.redaction import redact

logger = logging.getLogger(__name__)
router = APIRouter()

SIGNATURE_HEADER = "sentry-hook-signature"
RESOURCE_HEADER = "sentry-hook-resource"
ALERT_RESOURCE = "event_alert"
#: An issue alert carries the whole event, frames included; well under this.
MAX_BODY = 1024 * 1024
MAX_TITLE = 200


@dataclass
class AlertRelay:
    """What the route needs, wired once by the app factory."""

    secret: str = field(repr=False)
    notifier: AlertNotifier
    clock: Clock
    gate: RelayGate = field(default_factory=RelayGate)

    async def forward(self, alert: Alert) -> None:
        started = time.monotonic()
        fields: dict[str, object] = {"issue_id": alert.issue_id, "alert_level": alert.level}
        try:
            await self.notifier.notify(alert)
        except Exception as error:
            # Warning, never error: an error line would become a Sentry event.
            metrics.count_relay_failure()
            logger.warning(
                "alert.relay_failed",
                extra={**fields, "error_type": type(error).__name__, "duration_ms": _ms(started)},
            )
            return
        logger.info("alert.relay_forwarded", extra={**fields, "duration_ms": _ms(started)})


def alert_relay(request: Request) -> AlertRelay | None:
    return cast(AlertRelay | None, getattr(request.app.state, "alert_relay", None))


@router.post(RELAY_PATH, include_in_schema=False)
async def receive_sentry_alert(request: Request, background: BackgroundTasks) -> Response:
    relay = alert_relay(request)
    if relay is None:
        # SENTRY_WEBHOOK_SECRET unset: the route does not exist.
        raise ApiError(404, "not_found")
    content_length = request.headers.get("Content-Length")
    if content_length is not None and content_length.isdigit() and int(content_length) > MAX_BODY:
        raise ApiError(413, "payload_too_large")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise ApiError(413, "payload_too_large")
    if not signature_valid(body, request.headers.get(SIGNATURE_HEADER), relay.secret):
        raise ApiError(401, "invalid_signature")

    resource = request.headers.get(RESOURCE_HEADER, "")
    if resource != ALERT_RESOURCE:
        # Signed but not an alert, e.g. the integration's installation event.
        logger.info("alert.relay_ignored", extra={"resource": resource[:40]})
        return Response(status_code=204)
    try:
        alert = parse_sentry_alert(json.loads(body))
    except ValueError:
        raise ApiError(400, "invalid_payload") from None

    reason = relay.gate.admit(alert, relay.clock.now())
    if reason is not None:
        metrics.count_relay_drop(reason)
        logger.warning(
            "alert.relay_dropped", extra={"issue_id": alert.issue_id, "reason": reason.value}
        )
        return Response(status_code=202)
    background.add_task(relay.forward, alert)
    return Response(status_code=202)


def signature_valid(body: bytes, header: str | None, secret: str) -> bool:
    """HMAC-SHA256 of the received bytes with the integration's client secret, hex."""
    if not header:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip().lower())


def parse_sentry_alert(payload: object) -> Alert:
    """The fields of an ``event_alert`` delivery the message needs. Raises ValueError
    when the event or its ids are missing."""
    data = payload.get("data") if isinstance(payload, Mapping) else None
    event = data.get("event") if isinstance(data, Mapping) else None
    if not isinstance(data, Mapping) or not isinstance(event, Mapping):
        raise ValueError("data.event missing")
    issue_id, event_id = _text(event.get("issue_id"), 32), _text(event.get("event_id"), 64)
    if not issue_id or not event_id:
        raise ValueError("issue_id or event_id missing")
    link = _text(event.get("web_url"), 500)
    return Alert(
        issue_id=issue_id,
        event_id=event_id,
        level=_text(event.get("level"), 16) or "error",
        # The issue title: the exception type and error_kind in production, still
        # untrusted text, so redacted and clipped like a log value.
        title=redact(_text(event.get("title"), 1000) or "(no title)")[0][:MAX_TITLE],
        rule=_text(data.get("triggered_rule"), 100) or "",
        environment=_text(event.get("environment") or _tag(event, "environment"), 64),
        release=_text(event.get("release") or _tag(event, "release"), 64),
        link=link if link and link.startswith("https://") else None,
        occurred_at=_occurred_at(event),
    )


def _occurred_at(event: Mapping[str, Any]) -> datetime | None:
    """The event's own time: ``timestamp`` (epoch seconds), else ``datetime`` (ISO 8601),
    as Sentry documents both (⚠️ confirmed against a real delivery, checklist)."""
    timestamp = event.get("timestamp")
    if isinstance(timestamp, int | float) and not isinstance(timestamp, bool):
        return datetime.fromtimestamp(timestamp, UTC)
    raw = event.get("datetime")
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _tag(event: Mapping[str, Any], key: str) -> object:
    """A tag value; Sentry sends tags as [key, value] pairs, or as a mapping."""
    tags = event.get("tags")
    if isinstance(tags, Mapping):
        return tags.get(key)
    if isinstance(tags, list):
        for pair in tags:
            if isinstance(pair, list | tuple) and len(pair) == 2 and pair[0] == key:
                return pair[1]
    return None


def _text(value: object, limit: int) -> str | None:
    if value is None or isinstance(value, bool) or not isinstance(value, str | int):
        return None
    text = str(value).strip()
    return text[:limit] or None


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
