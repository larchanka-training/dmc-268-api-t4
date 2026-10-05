"""GitHub identity adapter: the App's user-to-server sign-in flow.

GitHub Apps have no OAuth scopes. Reading a user's role in an organisation needs
the App's "Organization permissions -> Members: Read-only" permission instead.

The token this adapter receives is used for the profile calls and then dropped:
never stored, never logged, never put in an exception message.
"""

import logging
from typing import Any
from urllib.parse import urlencode

import httpx

from adapters.identity.config import PROVIDER_NAME, GitHubIdentitySettings
from domain.errors import IdentityProviderError
from domain.ports import Profile, Token
from domain.tenancy import AccountType, Organization, OrganizationRole, User

logger = logging.getLogger(__name__)

_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
# GitHub's own role names, which are not ours.
_ROLE_BY_GITHUB_ROLE = {"admin": OrganizationRole.OWNER, "member": OrganizationRole.MEMBER}
_ACCOUNT_TYPE_BY_GITHUB_TYPE = {
    "Organization": AccountType.ORGANIZATION,
    "User": AccountType.USER,
}
_MEMBERSHIP_UNREADABLE = (403, 404)


class GitHubIdentityProvider:
    """Implements domain.ports.IdentityProvider against github.com."""

    def __init__(
        self,
        settings: GitHubIdentitySettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    def authorize_url(self, state: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "client_id": self._settings.client_id,
                "redirect_uri": self._settings.callback_url,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._settings.authorize_url}?{query}"

    async def exchange_code(self, code: str, code_verifier: str) -> Token:
        payload = {
            "client_id": self._settings.client_id,
            "client_secret": self._settings.client_secret,
            "code": code,
            "redirect_uri": self._settings.callback_url,
            "code_verifier": code_verifier,
        }
        async with self._client() as client:
            response = await self._send(
                client, "POST", self._settings.token_url, data=payload, auth_token=None
            )
        body = self._json(response, "token endpoint")
        error = body.get("error")
        if error:
            # GitHub answers 200 with an error field. The description may echo
            # request values, so only the error code is reported.
            raise IdentityProviderError(PROVIDER_NAME, f"token exchange rejected: {error}")
        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise IdentityProviderError(PROVIDER_NAME, "token endpoint returned no access_token")
        expires_in = body.get("expires_in")
        refresh_token = body.get("refresh_token")
        return Token(
            access_token=access_token,
            token_type=str(body.get("token_type") or "bearer"),
            expires_in=expires_in if isinstance(expires_in, int) else None,
            refresh_token=refresh_token if isinstance(refresh_token, str) else None,
        )

    async def load_profile(self, token: Token) -> Profile:
        async with self._client() as client:
            user = self._map_user(
                self._json(
                    await self._get(client, "/user", token),
                    "/user",
                )
            )
            installations = self._json(
                await self._get(client, "/user/installations", token),
                "/user/installations",
            ).get("installations")
            organizations = await self._map_organizations(client, token, user, installations)
        logger.info(
            "github profile loaded",
            extra={"provider": PROVIDER_NAME, "organizations": len(organizations)},
        )
        return Profile(user=user, organizations=organizations)

    # --- mapping ---------------------------------------------------------

    @staticmethod
    def _map_user(body: dict[str, Any]) -> User:
        login = str(body.get("login") or "").strip()
        if not login:
            raise IdentityProviderError(PROVIDER_NAME, "/user returned no login")
        identifier = body.get("id")
        if identifier is None:
            raise IdentityProviderError(PROVIDER_NAME, "/user returned no id")
        name = body.get("name")
        return User(
            id=str(identifier),
            login=login,
            # GitHub returns null for an unset display name.
            name=str(name) if isinstance(name, str) and name.strip() else login,
            avatar_url=str(body.get("avatar_url") or ""),
        )

    async def _map_organizations(
        self,
        client: httpx.AsyncClient,
        token: Token,
        user: User,
        installations: Any,
    ) -> tuple[Organization, ...]:
        if not isinstance(installations, list):
            return ()
        organizations: list[Organization] = []
        for installation in installations:
            if not isinstance(installation, dict):
                continue
            account = installation.get("account")
            installation_id = installation.get("id")
            if not isinstance(account, dict) or not isinstance(installation_id, int):
                continue
            organization = await self._map_account(client, token, user, account, installation_id)
            if organization is not None:
                organizations.append(organization)
        return tuple(organizations)

    async def _map_account(
        self,
        client: httpx.AsyncClient,
        token: Token,
        user: User,
        account: dict[str, Any],
        installation_id: int,
    ) -> Organization | None:
        login = str(account.get("login") or "").strip()
        identifier = account.get("id")
        if not login or identifier is None:
            return None
        account_type = account.get("type")
        mapped_type = _ACCOUNT_TYPE_BY_GITHUB_TYPE.get(str(account_type))
        if mapped_type is None:
            return None

        if mapped_type is AccountType.USER:
            # The App installed on the user's own account: the account-match rule.
            if str(identifier) != user.id:
                return None
            role = OrganizationRole.OWNER
        else:
            resolved = await self._organization_role(client, token, login)
            if resolved is None:
                return None
            role = resolved

        name = account.get("name")
        return Organization(
            id=str(identifier),
            login=login,
            name=str(name) if isinstance(name, str) and name.strip() else login,
            avatar_url=str(account.get("avatar_url") or ""),
            role=role,
            installation_id=installation_id,
            account_type=mapped_type,
        )

    async def _organization_role(
        self, client: httpx.AsyncClient, token: Token, login: str
    ) -> OrganizationRole | None:
        """None when the membership cannot be read or is not active.

        One unreadable organisation drops that organisation rather than failing
        the whole sign-in: the user still gets the accounts we can vouch for.
        """
        response = await self._get(
            client, f"/user/memberships/orgs/{login}", token, allow_status=_MEMBERSHIP_UNREADABLE
        )
        if response.status_code in _MEMBERSHIP_UNREADABLE:
            logger.warning(
                "github membership unreadable",
                extra={"provider": PROVIDER_NAME, "status": response.status_code},
            )
            return None
        body = self._json(response, "/user/memberships/orgs")
        if body.get("state") != "active":
            return None
        return _ROLE_BY_GITHUB_ROLE.get(str(body.get("role") or ""))

    # --- transport -------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._settings.timeout_seconds,
            transport=self._transport,
            follow_redirects=False,
        )

    async def _get(
        self,
        client: httpx.AsyncClient,
        path: str,
        token: Token,
        *,
        allow_status: tuple[int, ...] = (),
    ) -> httpx.Response:
        return await self._send(
            client,
            "GET",
            f"{self._settings.api_base_url}{path}",
            auth_token=token,
            allow_status=allow_status,
        )

    async def _send(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        auth_token: Token | None,
        data: dict[str, str] | None = None,
        allow_status: tuple[int, ...] = (),
    ) -> httpx.Response:
        headers = dict(_API_HEADERS)
        if auth_token is not None:
            headers["Authorization"] = f"Bearer {auth_token.access_token}"
        if data is not None:
            headers["Accept"] = "application/json"
        try:
            response = await client.request(method, url, data=data, headers=headers)
        except httpx.TimeoutException as error:
            raise IdentityProviderError(PROVIDER_NAME, f"{method} timed out") from error
        except httpx.HTTPError as error:
            # type(error).__name__ only: the message can contain the URL.
            raise IdentityProviderError(
                PROVIDER_NAME, f"{method} failed: {type(error).__name__}"
            ) from error
        if response.status_code >= 400 and response.status_code not in allow_status:
            raise IdentityProviderError(
                PROVIDER_NAME, f"{method} returned HTTP {response.status_code}"
            )
        return response

    @staticmethod
    def _json(response: httpx.Response, what: str) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as error:
            raise IdentityProviderError(PROVIDER_NAME, f"{what} returned invalid JSON") from error
        if not isinstance(body, dict):
            raise IdentityProviderError(PROVIDER_NAME, f"{what} returned a non-object body")
        return body
