import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from domain.errors import ForgeConfigurationError

DEFAULT_API_BASE_URL = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 15.0
PROVIDER_NAME = "github"


@dataclass(frozen=True, slots=True)
class GitHubAppSettings:
    """Credentials for acting as the App itself, rather than as a user."""

    app_id: str
    # The key's text, read from a file at startup. The path, not the key, is the
    # environment variable: a signing key never belongs in the environment.
    private_key: str = field(repr=False)
    app_slug: str
    # Verifies webhook deliveries; a secret, so it never appears in a repr.
    webhook_secret: str = field(repr=False)
    api_base_url: str = DEFAULT_API_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GitHubAppSettings":
        source = os.environ if env is None else env

        def text(name: str) -> str:
            return source.get(name, "").strip()

        required = (
            "GITHUB_APP_ID",
            "GITHUB_APP_PRIVATE_KEY_PATH",
            "GITHUB_APP_SLUG",
            "GITHUB_WEBHOOK_SECRET",
        )
        missing = [name for name in required if not text(name)]
        if missing:
            raise ForgeConfigurationError(
                "missing required environment variable(s): " + ", ".join(missing)
            )

        key_path = Path(text("GITHUB_APP_PRIVATE_KEY_PATH"))
        try:
            private_key = key_path.read_text(encoding="utf-8")
        except OSError as error:
            # The path, never the contents, and no OSError text that might carry more.
            raise ForgeConfigurationError(
                f"GITHUB_APP_PRIVATE_KEY_PATH cannot be read: {key_path}"
            ) from error
        if "PRIVATE KEY" not in private_key:
            raise ForgeConfigurationError(
                f"GITHUB_APP_PRIVATE_KEY_PATH is not a PEM private key: {key_path}"
            )

        return cls(
            app_id=text("GITHUB_APP_ID"),
            private_key=private_key,
            app_slug=text("GITHUB_APP_SLUG"),
            webhook_secret=text("GITHUB_WEBHOOK_SECRET"),
            api_base_url=(text("GITHUB_API_BASE_URL") or DEFAULT_API_BASE_URL).rstrip("/"),
        )
