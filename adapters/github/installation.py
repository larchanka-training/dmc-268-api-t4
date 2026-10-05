"""Listing the repositories an installation can see."""

import logging
from typing import Any

import httpx

from adapters.github.app_auth import GitHubAppAuth
from adapters.github.config import PROVIDER_NAME, GitHubAppSettings
from domain.errors import ForgeUnavailableError, InstallationGoneError
from domain.repositories import Repository

logger = logging.getLogger(__name__)

PER_PAGE = 100
# Ten times the design's stated scale ("under 100 repositories"). A cap, not a
# limit we expect to reach: without it a bad total_count would loop forever.
MAX_PAGES = 10
DEFAULT_BRANCH_FALLBACK = "main"

_UNINSTALLED_OR_SUSPENDED = (403, 404)


class GitHubInstallationGateway:
    """Implements domain.ports.InstallationGateway against github.com."""

    def __init__(
        self,
        settings: GitHubAppSettings,
        app_auth: GitHubAppAuth,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._app_auth = app_auth
        self._transport = transport

    async def list_repositories(self, installation_id: int) -> tuple[Repository, ...]:
        token = await self._app_auth.installation_token(installation_id)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{self._settings.api_base_url}/installation/repositories"

        repositories: list[Repository] = []
        total: int | None = None
        async with httpx.AsyncClient(
            timeout=self._settings.timeout_seconds, transport=self._transport
        ) as client:
            for page_number in range(1, MAX_PAGES + 1):
                body = await self._fetch_page(client, url, headers, installation_id, page_number)
                if total is None:
                    reported = body.get("total_count")
                    total = reported if isinstance(reported, int) else None
                batch = body.get("repositories")
                if not isinstance(batch, list) or not batch:
                    break
                repositories.extend(
                    mapped for mapped in (self._map(item) for item in batch) if mapped is not None
                )
                if total is not None and len(repositories) >= total:
                    break
                if len(batch) < PER_PAGE:
                    break
            else:
                logger.warning(
                    "repository listing hit the page cap",
                    extra={
                        "provider": PROVIDER_NAME,
                        "installation_id": installation_id,
                        "pages": MAX_PAGES,
                    },
                )

        logger.info(
            "repositories listed",
            extra={
                "provider": PROVIDER_NAME,
                "installation_id": installation_id,
                "count": len(repositories),
            },
        )
        return tuple(repositories)

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        installation_id: int,
        page_number: int,
    ) -> dict[str, Any]:
        params = {"per_page": str(PER_PAGE), "page": str(page_number)}
        try:
            response = await client.get(url, headers=headers, params=params)
        except httpx.TimeoutException as error:
            raise ForgeUnavailableError(PROVIDER_NAME, "repository listing timed out") from error
        except httpx.HTTPError as error:
            raise ForgeUnavailableError(
                PROVIDER_NAME, f"repository listing failed: {type(error).__name__}"
            ) from error

        if response.status_code in _UNINSTALLED_OR_SUSPENDED:
            raise InstallationGoneError(installation_id, f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ForgeUnavailableError(
                PROVIDER_NAME, f"repository listing returned HTTP {response.status_code}"
            )
        try:
            body = response.json()
        except ValueError as error:
            raise ForgeUnavailableError(
                PROVIDER_NAME, "repository listing returned invalid JSON"
            ) from error
        if not isinstance(body, dict):
            raise ForgeUnavailableError(
                PROVIDER_NAME, "repository listing returned a non-object body"
            )
        return body

    @staticmethod
    def _map(item: Any) -> Repository | None:
        if not isinstance(item, dict):
            return None
        identifier = item.get("id")
        name = item.get("name")
        owner = item.get("owner")
        login = owner.get("login") if isinstance(owner, dict) else None
        if identifier is None or not isinstance(name, str) or not isinstance(login, str):
            return None
        branch = item.get("default_branch")
        return Repository(
            id=str(identifier),
            owner=login,
            name=name,
            private=bool(item.get("private")),
            # GitHub omits default_branch for an empty repository.
            default_branch=(
                branch if isinstance(branch, str) and branch else DEFAULT_BRANCH_FALLBACK
            ),
            html_url=str(item.get("html_url") or ""),
            connected_at=None,
            last_run_at=None,
        )
