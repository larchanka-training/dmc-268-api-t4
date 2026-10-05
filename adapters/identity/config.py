import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from domain.errors import AuthConfigurationError

DEFAULT_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
DEFAULT_TOKEN_URL = "https://github.com/login/oauth/access_token"
DEFAULT_API_BASE_URL = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 10.0

PROVIDER_NAME = "github"


@dataclass(frozen=True, slots=True)
class GitHubIdentitySettings:
    """Credentials and endpoints for the App's user-to-server flow."""

    client_id: str
    client_secret: str = field(repr=False)
    # The public URL registered on GitHub, compared character for character.
    # Explicit rather than derived: the backend never sees the /api prefix.
    callback_url: str
    authorize_url: str = DEFAULT_AUTHORIZE_URL
    token_url: str = DEFAULT_TOKEN_URL
    api_base_url: str = DEFAULT_API_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GitHubIdentitySettings":
        source = os.environ if env is None else env

        def text(name: str) -> str:
            return source.get(name, "").strip()

        required = ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "GITHUB_CALLBACK_URL")
        missing = [name for name in required if not text(name)]
        if missing:
            raise AuthConfigurationError(
                "missing required environment variable(s): " + ", ".join(missing)
            )

        callback_url = text("GITHUB_CALLBACK_URL")
        if not callback_url.startswith(("http://", "https://")):
            raise AuthConfigurationError(
                f"GITHUB_CALLBACK_URL must be an http(s) URL, got '{callback_url}'"
            )

        return cls(
            client_id=text("GITHUB_CLIENT_ID"),
            client_secret=text("GITHUB_CLIENT_SECRET"),
            callback_url=callback_url,
            api_base_url=(text("GITHUB_API_BASE_URL") or DEFAULT_API_BASE_URL).rstrip("/"),
        )
