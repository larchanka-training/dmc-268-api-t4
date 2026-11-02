"""Where the alert relay sends an alert: Telegram, or nowhere until the bot exists.

docs/observability.md § Alerts, "The relay". The bot token is part of the Bot API's URL
path, which httpx logs; domain/redaction.py's telegram_bot_token rule takes it out of
every log line and Sentry event, and the token never appears in an exception here.
"""

import logging

import httpx

from adapters.alerting.config import AlertingSettings
from domain.alerts import Alert
from domain.ports import AlertNotifier

TELEGRAM_API = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 10.0

logger = logging.getLogger(__name__)


class TelegramError(Exception):
    """sendMessage did not deliver the message. The text never carries the token."""


def render(alert: Alert) -> str:
    """The message: plain text, so nothing in the title is read as markup."""
    context = " · ".join(
        part
        for part in (
            alert.rule,
            alert.environment,
            f"release {alert.release[:12]}" if alert.release else None,
        )
        if part
    )
    lines = [f"[{alert.level.upper()}] {context}" if context else f"[{alert.level.upper()}]"]
    lines.append(alert.title)
    if alert.link:
        lines.append(alert.link)
    return "\n".join(lines)


class TelegramNotifier:
    """Posts each alert with the Bot API's sendMessage."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = TELEGRAM_API,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._transport = transport
        self._base_url = base_url
        self._timeout = timeout_seconds

    def __repr__(self) -> str:
        return "TelegramNotifier()"

    async def notify(self, alert: Alert) -> None:
        async with httpx.AsyncClient(
            base_url=self._base_url, transport=self._transport, timeout=self._timeout
        ) as client:
            response = await client.post(
                f"/bot{self._token}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": render(alert),
                    "link_preview_options": {"is_disabled": True},
                },
            )
        try:
            delivered = response.json().get("ok") is True
        except ValueError:
            delivered = False
        if response.status_code != 200 or not delivered:
            raise TelegramError(f"sendMessage answered HTTP {response.status_code}")


class NullNotifier:
    """The placeholder while ALERTS_TELEGRAM_BOT_TOKEN or ALERTS_TELEGRAM_CHAT_ID is
    unset: the whole path runs, and the message is dropped with a log line instead of
    sent."""

    async def notify(self, alert: Alert) -> None:
        logger.info("alert.dropped", extra={"issue_id": alert.issue_id, "alert_level": alert.level})


def build_notifier(
    settings: AlertingSettings, *, transport: httpx.AsyncBaseTransport | None = None
) -> AlertNotifier:
    if not settings.telegram_enabled:
        return NullNotifier()
    return TelegramNotifier(
        settings.telegram_bot_token, settings.telegram_chat_id, transport=transport
    )
