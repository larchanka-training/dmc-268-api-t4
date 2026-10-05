import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from domain.errors import AuthConfigurationError

DEFAULT_SESSION_TTL_SECONDS = 3600
DEFAULT_OAUTH_STATE_TTL_SECONDS = 600
DEFAULT_REPOSITORIES_CACHE_TTL_SECONDS = 60


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Startup settings for the API process. Not review policy: that lives in the
    database (docs/configuration.md)."""

    app_origin: str
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    oauth_state_ttl_seconds: int = DEFAULT_OAUTH_STATE_TTL_SECONDS
    repositories_cache_ttl_seconds: int = DEFAULT_REPOSITORIES_CACHE_TTL_SECONDS

    @property
    def session_ttl(self) -> timedelta:
        return timedelta(seconds=self.session_ttl_seconds)

    @property
    def oauth_state_ttl(self) -> timedelta:
        return timedelta(seconds=self.oauth_state_ttl_seconds)

    @property
    def repositories_cache_ttl(self) -> timedelta:
        return timedelta(seconds=self.repositories_cache_ttl_seconds)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AppSettings":
        source = os.environ if env is None else env

        def text(name: str) -> str:
            return source.get(name, "").strip()

        origin = text("APP_ORIGIN")
        if not origin:
            raise AuthConfigurationError("missing required environment variable(s): APP_ORIGIN")
        if not origin.startswith(("http://", "https://")):
            raise AuthConfigurationError(f"APP_ORIGIN must be an http(s) URL, got '{origin}'")

        def seconds(name: str, default: int) -> int:
            raw = text(name)
            if not raw:
                return default
            try:
                value = int(raw)
            except ValueError as error:
                raise AuthConfigurationError(
                    f"{name} must be a whole number of seconds, got '{raw}'"
                ) from error
            if value <= 0:
                raise AuthConfigurationError(f"{name} must be greater than 0, got {value}")
            return value

        return cls(
            app_origin=origin.rstrip("/"),
            session_ttl_seconds=seconds("SESSION_TTL_SECONDS", DEFAULT_SESSION_TTL_SECONDS),
            oauth_state_ttl_seconds=seconds(
                "OAUTH_STATE_TTL_SECONDS", DEFAULT_OAUTH_STATE_TTL_SECONDS
            ),
            repositories_cache_ttl_seconds=seconds(
                "REPOSITORIES_CACHE_TTL_SECONDS", DEFAULT_REPOSITORIES_CACHE_TTL_SECONDS
            ),
        )
