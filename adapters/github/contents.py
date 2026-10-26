"""Reading one file of a repository at a ref (`GET /repos/{owner}/{repo}/contents/{path}`).

Used for `.review/rules.md` (docs/RULES_FORMAT_SPEC.md §1, §5). The body is repository
content: it is returned to the caller and never logged, and error messages carry a
status and a reason only (AGENTS.md hard rules 2 and 3).
"""

import logging
import time
from urllib.parse import quote

import httpx

from adapters.github.app_auth import GitHubAppAuth
from adapters.github.config import PROVIDER_NAME, GitHubAppSettings
from domain.errors import ForgePermissionError, ForgeRateLimitedError, ForgeUnavailableError

logger = logging.getLogger(__name__)

_STEP = "contents fetch"


class GitHubRepositoryContents:
    """Implements domain.ports.RepositoryContents against github.com."""

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

    async def read_file(
        self,
        installation_id: int,
        repo_full_name: str,
        path: str,
        ref: str,
        *,
        max_bytes: int,
    ) -> bytes | None:
        owner, _, name = repo_full_name.partition("/")
        if not owner or not name or "/" in name:
            raise ForgeUnavailableError(
                PROVIDER_NAME, "invalid repo full name: expected owner/repo"
            )
        started = time.monotonic()
        token = await self._app_auth.installation_token(installation_id)
        url = (
            f"{self._settings.api_base_url.rstrip('/')}/repos/{quote(owner, safe='')}"
            f"/{quote(name, safe='')}/contents/{quote(path)}"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            # The raw media type returns the file itself, not base64 inside JSON.
            "Accept": "application/vnd.github.raw+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(
            timeout=self._settings.timeout_seconds,
            transport=self._transport,
            follow_redirects=True,
        ) as client:
            request = client.build_request("GET", url, headers=headers, params={"ref": ref})
            try:
                response = await client.send(request, stream=True)
            except httpx.TimeoutException as error:
                raise ForgeUnavailableError(PROVIDER_NAME, f"{_STEP} timed out") from error
            except httpx.HTTPError as error:
                raise ForgeUnavailableError(
                    PROVIDER_NAME, f"{_STEP} failed: network error ({type(error).__name__})"
                ) from error
            try:
                if response.status_code == 404:
                    content = None
                else:
                    _check_status(response)
                    content = await _read_capped(response, max_bytes)
            finally:
                await response.aclose()

        logger.info(
            "github.file.read",
            extra={
                "provider": PROVIDER_NAME,
                "installation_id": installation_id,
                "repo": repo_full_name,
                "found": content is not None,
                "bytes": 0 if content is None else len(content),
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return content


async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    """At most max_bytes + 1 bytes: enough to tell the caller the file is too large."""
    limit = max_bytes + 1
    chunks: list[bytes] = []
    received = 0
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        received += len(chunk)
        if received >= limit:
            break
    return b"".join(chunks)[:limit]


def _check_status(response: httpx.Response) -> None:
    """Never a response body in the message: it may hold repository content."""
    status = response.status_code
    if status == 429 or (status == 403 and response.headers.get("x-ratelimit-remaining") == "0"):
        retry_after = response.headers.get("retry-after", "")
        raise ForgeRateLimitedError(
            PROVIDER_NAME, int(retry_after) if retry_after.isdigit() else None
        )
    if status in (401, 403):
        raise ForgePermissionError(PROVIDER_NAME, f"{_STEP}: HTTP {status}")
    if status != 200:
        raise ForgeUnavailableError(PROVIDER_NAME, f"{_STEP}: HTTP {status}")
