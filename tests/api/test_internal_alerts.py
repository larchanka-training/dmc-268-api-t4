"""api/internal_alerts.py: the Sentry → Telegram relay route (docs/observability.md §
Alerts, "The relay"). Deliveries are built like Sentry's documented ``event_alert``
payload and signed with the test secret; Telegram is ``httpx.MockTransport``."""

import hashlib
import hmac
import io
import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import pytest
import sentry_sdk
from fastapi.testclient import TestClient

from adapters.alerting.telegram import TelegramNotifier
from adapters.observability import metrics
from adapters.observability.sentry import RELAY_PATH, traces_sampler
from api.internal_alerts import AlertRelay
from domain.alerts import RATE_LIMIT, Alert
from tests.conftest import (
    CaptureTransport,
    FakeIdentityProvider,
    FixedClock,
    build_test_app,
    json_lines,
)

SECRET = "sentry-client-secret-for-tests"
TELEGRAM_TOKEN = "123456789:" + "AAH" + "x1Y2z3" * 5 + "_-"


@dataclass
class RecordingNotifier:
    sent: list[Alert] = field(default_factory=list)
    error: Exception | None = None

    async def notify(self, alert: Alert) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(alert)


def delivery(
    clock: FixedClock, *, event_id: str = "e4874d664c3540c1a32eab185f12c5ab", **event: Any
) -> dict[str, Any]:
    return {
        "action": "triggered",
        "actor": {"id": "sentry", "name": "Sentry", "type": "application"},
        "data": {
            "event": {
                "event_id": event_id,
                "issue_id": "1117540176",
                "level": "error",
                "title": "ForgeUnavailableError: forge_unavailable",
                "timestamp": clock.now().timestamp(),
                "datetime": clock.now().isoformat(),
                "release": "a" * 40,
                "tags": [["environment", "production"], ["level", "error"]],
                "web_url": "https://de.sentry.io/organizations/o/issues/1117540176/events/e48/",
                **event,
            },
            "triggered_rule": "New issue",
        },
        "installation": {"uuid": "7a485448-a9e2-4c85-8a3c-4f44175783c9"},
    }


def post(
    client: TestClient,
    payload: object,
    *,
    secret: str = SECRET,
    signature: str | None = None,
    resource: str = "event_alert",
) -> httpx.Response:
    body = json.dumps(payload).encode()
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        RELAY_PATH,
        content=body,
        headers={
            "Content-Type": "application/json",
            "Sentry-Hook-Resource": resource,
            "Sentry-Hook-Signature": digest if signature is None else signature,
        },
    )


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def client(auth_clock: FixedClock, notifier: RecordingNotifier) -> TestClient:
    relay = AlertRelay(secret=SECRET, notifier=notifier, clock=auth_clock)
    # No Origin and no X-Requested-With, as Sentry sends it: the route is CSRF-exempt.
    return TestClient(build_test_app(auth_clock, FakeIdentityProvider(), alert_relay=relay))


def dropped(reason: str) -> float:
    return metrics.ALERT_RELAY_DROPPED.labels(reason)._value.get()


def test_without_a_secret_the_route_does_not_exist(auth_clock: FixedClock) -> None:
    client = TestClient(build_test_app(auth_clock, FakeIdentityProvider()))

    assert post(client, delivery(auth_clock)).status_code == 404


def test_a_signed_alert_is_forwarded(
    client: TestClient, notifier: RecordingNotifier, auth_clock: FixedClock
) -> None:
    response = post(client, delivery(auth_clock))

    assert response.status_code == 202
    [alert] = notifier.sent
    assert alert.issue_id == "1117540176"
    assert alert.title == "ForgeUnavailableError: forge_unavailable"
    assert alert.rule == "New issue"
    assert alert.environment == "production"  # from the tags
    assert alert.release == "a" * 40
    assert alert.link and alert.link.startswith("https://de.sentry.io/")
    assert alert.occurred_at == auth_clock.now()


@pytest.mark.parametrize(
    ("secret", "signature"),
    [("another-secret", None), (SECRET, ""), (SECRET, "0" * 64)],
)
def test_a_bad_or_missing_signature_is_401(
    client: TestClient,
    notifier: RecordingNotifier,
    auth_clock: FixedClock,
    secret: str,
    signature: str | None,
) -> None:
    response = post(client, delivery(auth_clock), secret=secret, signature=signature)

    assert response.status_code == 401
    assert notifier.sent == []


def test_a_signed_delivery_that_is_not_an_alert_is_ignored(
    client: TestClient, notifier: RecordingNotifier
) -> None:
    response = post(client, {"action": "created", "data": {}}, resource="installation")

    assert response.status_code == 204
    assert notifier.sent == []


def test_a_signed_alert_without_its_event_is_400(client: TestClient) -> None:
    assert post(client, {"action": "triggered", "data": {}}).status_code == 400


def test_an_oversized_body_is_413(client: TestClient) -> None:
    response = client.post(RELAY_PATH, content=b"x" * (1024 * 1024 + 1))

    assert response.status_code == 413


def test_an_old_event_is_dropped_as_stale(
    client: TestClient,
    notifier: RecordingNotifier,
    auth_clock: FixedClock,
    json_logs: io.StringIO,
) -> None:
    before = dropped("stale")
    old = auth_clock.now() - timedelta(minutes=31)

    response = post(client, delivery(auth_clock, timestamp=old.timestamp()))

    assert response.status_code == 202
    assert notifier.sent == []
    assert dropped("stale") == before + 1
    [line] = [line for line in json_lines(json_logs) if line["event"] == "alert.relay_dropped"]
    assert line["level"] == "warning"
    assert line["reason"] == "stale"


def test_the_event_time_falls_back_on_datetime(
    client: TestClient, notifier: RecordingNotifier, auth_clock: FixedClock
) -> None:
    old = (auth_clock.now() - timedelta(hours=2)).isoformat()

    post(client, delivery(auth_clock, timestamp=None, datetime=old))

    assert notifier.sent == []


def test_a_repeated_delivery_is_dropped_as_duplicate(
    client: TestClient, notifier: RecordingNotifier, auth_clock: FixedClock
) -> None:
    before = dropped("duplicate")

    post(client, delivery(auth_clock))
    post(client, delivery(auth_clock))

    assert len(notifier.sent) == 1
    assert dropped("duplicate") == before + 1


def test_more_than_thirty_a_minute_are_dropped(
    client: TestClient, notifier: RecordingNotifier, auth_clock: FixedClock
) -> None:
    before = dropped("rate_limited")

    for index in range(RATE_LIMIT + 1):
        post(client, delivery(auth_clock, event_id=f"event-{index}"))

    assert len(notifier.sent) == RATE_LIMIT
    assert dropped("rate_limited") == before + 1


def test_the_title_is_redacted_and_clipped(
    client: TestClient, notifier: RecordingNotifier, auth_clock: FixedClock
) -> None:
    title = "ValueError: ghp_" + "A1b2C3d4E5" * 4 + " " + "x" * 300

    post(client, delivery(auth_clock, title=title))

    [alert] = notifier.sent
    assert "ghp_" not in alert.title
    assert "[REDACTED:github_token]" in alert.title
    assert len(alert.title) == 200


def test_a_link_that_is_not_https_is_left_out(
    client: TestClient, notifier: RecordingNotifier, auth_clock: FixedClock
) -> None:
    post(client, delivery(auth_clock, web_url="javascript:alert(1)"))

    assert notifier.sent[0].link is None


def test_a_failing_notifier_logs_a_warning_counts_it_and_reports_nothing_to_sentry(
    auth_clock: FixedClock, json_logs: io.StringIO, sentry_capture: CaptureTransport
) -> None:
    notifier = RecordingNotifier(error=RuntimeError("telegram is down"))
    relay = AlertRelay(secret=SECRET, notifier=notifier, clock=auth_clock)
    client = TestClient(build_test_app(auth_clock, FakeIdentityProvider(), alert_relay=relay))
    before = metrics.ALERT_RELAY_FAILURES._value.get()

    response = post(client, delivery(auth_clock))
    sentry_sdk.flush()

    assert response.status_code == 202
    assert metrics.ALERT_RELAY_FAILURES._value.get() == before + 1
    [line] = [line for line in json_lines(json_logs) if line["event"] == "alert.relay_failed"]
    assert line["level"] == "warning"
    assert line["error_type"] == "RuntimeError"
    assert sentry_capture.payloads("event") == []
    # Nor is the relay traced: its trace would carry the Bot API URL.
    assert sentry_capture.payloads("transaction") == []


def test_a_crash_inside_the_route_reaches_no_sentry_event(
    auth_clock: FixedClock, sentry_capture: CaptureTransport
) -> None:
    relay = AlertRelay(secret=SECRET, notifier=RecordingNotifier(), clock=auth_clock)

    def explode(*_: object) -> None:
        raise RuntimeError("bug in the relay")

    relay.gate.admit = explode  # type: ignore[method-assign]
    app = build_test_app(auth_clock, FakeIdentityProvider(), alert_relay=relay)
    client = TestClient(app, raise_server_exceptions=False)

    assert post(client, delivery(auth_clock)).status_code == 500
    sentry_sdk.flush()

    assert sentry_capture.payloads("event") == []


def test_the_relay_is_not_traced() -> None:
    assert traces_sampler({"asgi_scope": {"path": RELAY_PATH}}) == 0.0
    assert traces_sampler({"asgi_scope": {"path": "/api" + RELAY_PATH, "root_path": "/api"}}) == 0.0


def test_the_telegram_token_reaches_no_log_line(
    auth_clock: FixedClock, json_logs: io.StringIO
) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    notifier = TelegramNotifier(TELEGRAM_TOKEN, "-100", transport=httpx.MockTransport(handler))
    relay = AlertRelay(secret=SECRET, notifier=notifier, clock=auth_clock)
    client = TestClient(build_test_app(auth_clock, FakeIdentityProvider(), alert_relay=relay))

    post(client, delivery(auth_clock))

    assert len(sent) == 1
    logs = json_logs.getvalue()
    assert "alert.relay_forwarded" in logs
    assert TELEGRAM_TOKEN not in logs
