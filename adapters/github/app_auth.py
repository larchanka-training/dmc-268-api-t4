"""Acting as the GitHub App: a signed JWT, exchanged for installation tokens.

Neither the JWT nor an installation token is ever logged, returned by the API, or
put in an exception message (AGENTS.md hard rule 2).
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
import jwt

from adapters.github.config import PROVIDER_NAME, GitHubAppSettings
from domain.errors import ForgeUnavailableError, InstallationGoneError
from domain.ports import Clock

logger = logging.getLogger(__name__)

# GitHub allows at most 10 minutes; 9 leaves room for clock skew, and iat is
# backdated 60s because GitHub rejects a JWT issued in its future.
JWT_LIFETIME = timedelta(minutes=9)
JWT_BACKDATE = timedelta(seconds=60)
# Renew a token before it actually expires, so a long call cannot outlive it.
TOKEN_RENEW_MARGIN = timedelta(minutes=5)

_UNINSTALLED_OR_SUSPENDED = (403, 404)


@dataclass(frozen=True, slots=True)
class InstallationToken:
    value: str = field(repr=False)
    expires_at: datetime

    def is_usable(self, now: datetime) -> bool:
        return now + TOKEN_RENEW_MARGIN < self.expires_at


class GitHubAppAuth:
    """Mints installation tokens, caching each until shortly before it expires."""

    def __init__(
        self,
        settings: GitHubAppSettings,
        clock: Clock,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._transport = transport
        self._tokens: dict[int, InstallationToken] = {}

    def app_jwt(self) -> str:
        now = self._clock.now()
        return jwt.encode(
            {
                "iat": int((now - JWT_BACKDATE).timestamp()),
                "exp": int((now + JWT_LIFETIME).timestamp()),
                "iss": self._settings.app_id,
            },
            self._settings.private_key,
            algorithm="RS256",
        )

    async def installation_token(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        now = self._clock.now()
        if cached is not None and cached.is_usable(now):
            return cached.value

        token = await self._mint(installation_id)
        self._tokens[installation_id] = token
        return token.value

    def forget(self, installation_id: int) -> None:
        self._tokens.pop(installation_id, None)

    async def _mint(self, installation_id: int) -> InstallationToken:
        url = f"{self._settings.api_base_url}/app/installations/{installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {self.app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(
            timeout=self._settings.timeout_seconds, transport=self._transport
        ) as client:
            try:
                response = await client.post(url, headers=headers)
            except httpx.TimeoutException as error:
                raise ForgeUnavailableError(PROVIDER_NAME, "token request timed out") from error
            except httpx.HTTPError as error:
                raise ForgeUnavailableError(
                    PROVIDER_NAME, f"token request failed: {type(error).__name__}"
                ) from error

        if response.status_code in _UNINSTALLED_OR_SUSPENDED:
            self.forget(installation_id)
            raise InstallationGoneError(installation_id, f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ForgeUnavailableError(
                PROVIDER_NAME, f"token request returned HTTP {response.status_code}"
            )

        body = response.json() if response.content else {}
        if not isinstance(body, dict):
            raise ForgeUnavailableError(PROVIDER_NAME, "token response was not an object")
        value = body.get("token")
        expires_at_raw = body.get("expires_at")
        if not isinstance(value, str) or not value:
            raise ForgeUnavailableError(PROVIDER_NAME, "token response carried no token")
        logger.info(
            "installation token minted",
            extra={"provider": PROVIDER_NAME, "installation_id": installation_id},
        )
        return InstallationToken(value=value, expires_at=_parse_expiry(expires_at_raw))


def _parse_expiry(raw: object) -> datetime:
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as error:
            raise ForgeUnavailableError(
                PROVIDER_NAME, "token response carried an unparseable expires_at"
            ) from error
    raise ForgeUnavailableError(PROVIDER_NAME, "token response carried no expires_at")
