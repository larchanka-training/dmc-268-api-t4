"""adapters/alerting: the relay's settings and notifiers, Telegram against
``httpx.MockTransport`` (AGENTS.md hard rule 4)."""

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from adapters.alerting.config import AlertingSettings
from adapters.alerting.telegram import (
    NullNotifier,
    TelegramError,
    TelegramNotifier,
    build_notifier,
    render,
)
from domain.alerts import Alert

TOKEN = "123456789:" + "AAH" + "x1Y2z3" * 5 + "_-"
ALERT = Alert(
    issue_id="1117540176",
    event_id="e4874d664c3540c1a32eab185f12c5ab",
    level="error",
    title="ForgeUnavailableError: forge_unavailable",
    rule="Error burst",
    environment="production",
    release="0123456789abcdef" * 2 + "01234567",
    link="https://sentry.example/issues/1117540176/",
    occurred_at=datetime(2026, 10, 26, 12, 0, tzinfo=UTC),
)


def telegram(handler: httpx.MockTransport) -> TelegramNotifier:
    return TelegramNotifier(TOKEN, "-1001234", transport=handler)


def test_settings_read_the_environment_and_hide_the_secrets() -> None:
    settings = AlertingSettings.from_env(
        {
            "SENTRY_WEBHOOK_SECRET": " s3cret ",
            "ALERTS_TELEGRAM_BOT_TOKEN": TOKEN,
            "ALERTS_TELEGRAM_CHAT_ID": "1",
        }
    )

    assert settings.relay_enabled and settings.telegram_enabled
    assert settings.sentry_webhook_secret == "s3cret"
    assert "s3cret" not in repr(settings) and TOKEN not in repr(settings)


def test_unset_settings_turn_the_relay_and_telegram_off() -> None:
    settings = AlertingSettings.from_env({"ALERTS_TELEGRAM_BOT_TOKEN": TOKEN})

    assert not settings.relay_enabled
    assert not settings.telegram_enabled  # a token without a chat id is not enough


def test_build_notifier_picks_telegram_only_with_token_and_chat_id() -> None:
    assert isinstance(build_notifier(AlertingSettings()), NullNotifier)
    settings = AlertingSettings(telegram_bot_token=TOKEN, telegram_chat_id="1")
    notifier = build_notifier(settings)

    assert isinstance(notifier, TelegramNotifier)
    assert TOKEN not in repr(notifier)


def test_the_message_is_plain_text_with_the_rule_environment_release_and_link() -> None:
    assert render(ALERT) == (
        "[ERROR] Error burst · production · release 0123456789ab\n"
        "ForgeUnavailableError: forge_unavailable\n"
        "https://sentry.example/issues/1117540176/"
    )
    bare = replace(ALERT, rule="", environment=None, release=None, link=None)
    assert render(bare) == "[ERROR]\nForgeUnavailableError: forge_unavailable"


async def test_telegram_posts_send_message_to_the_chat() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True, "result": {}})

    await telegram(httpx.MockTransport(handler)).notify(ALERT)

    [request] = sent
    assert request.url.path == f"/bot{TOKEN}/sendMessage"
    body = json.loads(request.content)
    assert body["chat_id"] == "-1001234"
    assert body["text"] == render(ALERT)
    assert "parse_mode" not in body  # plain text: a title is never read as markup


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"ok": False, "description": "Unauthorized"}),
        httpx.Response(200, json={"ok": False}),
        httpx.Response(502, text="Bad Gateway"),
    ],
)
async def test_a_refused_message_raises_without_the_token(response: httpx.Response) -> None:
    notifier = telegram(httpx.MockTransport(lambda _: response))

    with pytest.raises(TelegramError) as raised:
        await notifier.notify(ALERT)

    assert TOKEN not in str(raised.value)
    assert f"HTTP {response.status_code}" in str(raised.value)


async def test_the_null_notifier_logs_the_drop_without_the_title(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        await NullNotifier().notify(ALERT)

    [record] = [r for r in caplog.records if r.getMessage() == "alert.dropped"]
    assert record.__dict__["issue_id"] == ALERT.issue_id
    assert record.__dict__["alert_level"] == "error"
    assert ALERT.title not in str(record.__dict__)
