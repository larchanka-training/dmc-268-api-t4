"""Settings of the Sentry → Telegram alert relay (docs/observability.md § Configuration
and secrets)."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class AlertingSettings:
    #: The client secret of the Sentry internal integration: it signs every delivery.
    #: Unset, the relay route answers 404.
    sentry_webhook_secret: str = field(default="", repr=False)
    #: Both unset (or either), alerts go to the NullNotifier, which logs alert.dropped.
    telegram_bot_token: str = field(default="", repr=False)
    telegram_chat_id: str = ""

    @property
    def relay_enabled(self) -> bool:
        return bool(self.sentry_webhook_secret)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AlertingSettings":
        source = os.environ if env is None else env

        def text(name: str) -> str:
            return source.get(name, "").strip()

        return cls(
            sentry_webhook_secret=text("SENTRY_WEBHOOK_SECRET"),
            telegram_bot_token=text("ALERTS_TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=text("ALERTS_TELEGRAM_CHAT_ID"),
        )
