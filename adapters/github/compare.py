"""Reading the diff between two commits (`GET /repos/{owner}/{repo}/compare/{base}...{head}`).

Used to rebuild the code behind a published finding at the revision it was reviewed at
(docs/PIPELINE_SPEC.md §7.4). The three-dot form diffs from the merge base, which is what
a pull request shows. The diff is returned to the caller and never stored or logged, and
error messages carry a status and a reason only (AGENTS.md hard rules 2 and 3).
"""

import logging
import time
from urllib.parse import quote

import httpx

from adapters.github.app_auth import GitHubAppAuth
from adapters.github.client import MAX_DIFF_BYTES
from adapters.github.config import PROVIDER_NAME, GitHubAppSettings
from domain.errors import ForgeRateLimitedError, ForgeUnavailableError, InstallationGoneError

logger = logging.getLogger(__name__)

_STEP = "compare fetch"


class GitHubCompareDiffs:
    """Implements domain.ports.CompareDiffs against github.com."""

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

    async def compare_diff(
        self, installation_id: int, repo_full_name: str, base_sha: str, head_sha: str
    ) -> str | None:
        owner, _, name = repo_full_name.partition("/")
        if not owner or not name or "/" in name:
            raise ForgeUnavailableError(
                PROVIDER_NAME, "invalid repo full name: expected owner/repo"
            )
        started = time.monotonic()
        token = await self._app_auth.installation_token(installation_id)
        url = (
            f"{self._settings.api_base_url.rstrip('/')}/repos/{quote(owner, safe='')}"
            f"/{quote(name, safe='')}/compare/{quote(base_sha, safe='')}"
            f"...{quote(head_sha, safe='')}"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.diff",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(
            timeout=self._settings.timeout_seconds,
            transport=self._transport,
            follow_redirects=True,
        ) as client:
            request = client.build_request("GET", url, headers=headers)
            try:
                response = await client.send(request, stream=True)
            except httpx.TimeoutException as error:
                raise ForgeUnavailableError(PROVIDER_NAME, f"{_STEP} timed out") from error
            except httpx.HTTPError as error:
                raise ForgeUnavailableError(
                    PROVIDER_NAME, f"{_STEP} failed: network error ({type(error).__name__})"
                ) from error
            try:
                # 404: a commit (or the repository) is gone; 422: GitHub cannot relate the
                # two commits any more, e.g. after a force-push dropped the base.
                if response.status_code in (404, 422):
                    text = None
                else:
                    _check_status(response, installation_id)
                    text = await _read_diff(response)
            finally:
                await response.aclose()

        logger.info(
            "github.compare.fetched",
            extra={
                "provider": PROVIDER_NAME,
                "installation_id": installation_id,
                "repo": repo_full_name,
                "found": text is not None,
                "diff_bytes": 0 if text is None else len(text.encode("utf-8")),
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return text


async def _read_diff(response: httpx.Response) -> str:
    declared_length = response.headers.get("Content-Length", "")
    if declared_length.isdigit() and int(declared_length) > MAX_DIFF_BYTES:
        raise ForgeUnavailableError(PROVIDER_NAME, f"diff exceeds {MAX_DIFF_BYTES} bytes")
    received = 0
    chunks: list[bytes] = []
    async for chunk in response.aiter_bytes():
        received += len(chunk)
        if received > MAX_DIFF_BYTES:
            raise ForgeUnavailableError(PROVIDER_NAME, f"diff exceeds {MAX_DIFF_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")


def _check_status(response: httpx.Response, installation_id: int) -> None:
    """Never a response body in the message: it may hold repository content."""
    status = response.status_code
    if status == 429 or (status == 403 and response.headers.get("x-ratelimit-remaining") == "0"):
        retry_after = response.headers.get("retry-after", "")
        raise ForgeRateLimitedError(
            PROVIDER_NAME, int(retry_after) if retry_after.isdigit() else None
        )
    if status in (401, 403):
        raise InstallationGoneError(installation_id, f"{_STEP}: HTTP {status}")
    if status != 200:
        raise ForgeUnavailableError(PROVIDER_NAME, f"{_STEP}: HTTP {status}")
