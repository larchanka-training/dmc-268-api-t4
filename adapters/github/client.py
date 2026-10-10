"""Fetching a pull request's state and diff from github.com.

Implements Step 5 of [docs/WORKFLOW_DESIGN.md §2](../../docs/WORKFLOW_DESIGN.md):
authenticate as the App (the machinery in `app_auth.py`), then read the pull
request, its commits and its unified diff — never a single-commit endpoint,
which would review one commit's diff instead of the whole range.

Error messages carry a status and a reason only: never a response body, which
may hold repository content, and never a token (AGENTS.md hard rule 2).
"""

import logging
import time
from typing import Any

import httpx

from adapters.github.app_auth import GitHubAppAuth
from adapters.github.config import PROVIDER_NAME, GitHubAppSettings
from domain.errors import ForgeUnavailableError, InstallationGoneError, PrNotFoundError
from domain.ports import CommitInfo, PullRequestContext

logger = logging.getLogger(__name__)

COMMITS_PER_PAGE = 100
# A cap, not a limit we expect to reach: without it a lying Link header would
# loop forever. 5 pages x 100 commits is far past any reviewable pull request.
MAX_COMMIT_PAGES = 5
MAX_DIFF_BYTES = 5 * 1024 * 1024

_PERMITTED_STATUSES = frozenset({200})


class GitHubVCSClient:
    """Implements domain.ports.GitProvider against github.com."""

    def __init__(
        self,
        settings: GitHubAppSettings,
        app_auth: GitHubAppAuth,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str | None = None,
    ) -> None:
        self._settings = settings
        self._app_auth = app_auth
        self._transport = transport
        self._base_url = (base_url if base_url is not None else settings.api_base_url).rstrip("/")

    async def fetch_pull_request(
        self, installation_id: int, repo_full_name: str, number: int
    ) -> PullRequestContext:
        _validate_repo_full_name(repo_full_name)
        started = time.monotonic()
        token = await self._app_auth.installation_token(installation_id)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(
            timeout=self._settings.timeout_seconds,
            transport=self._transport,
            follow_redirects=True,
        ) as client:
            detail = await self._get_json(
                client,
                f"/repos/{repo_full_name}/pulls/{number}",
                headers,
                "pull request fetch",
                installation_id,
            )
            commits, dropped_commits = await self._fetch_commits(
                client, repo_full_name, number, headers, installation_id
            )
            diff_path = f"/repos/{repo_full_name}/pulls/{number}.diff"
            diff_text = await self._fetch_diff(client, diff_path, headers, installation_id)
        context = _build_context(
            installation_id, repo_full_name, number, detail, commits, diff_text
        )
        logger.info(
            "pull request fetched",
            extra={
                "provider": PROVIDER_NAME,
                "installation_id": installation_id,
                "repo": repo_full_name,
                "pr_number": number,
                "sha": context.head_sha,
                "commits": len(context.commits),
                "dropped_commits": dropped_commits,
                "diff_bytes": len(diff_text.encode("utf-8")),
                "duration_seconds": round(time.monotonic() - started, 3),
            },
        )
        return context

    async def _fetch_commits(
        self,
        client: httpx.AsyncClient,
        repo_full_name: str,
        number: int,
        headers: dict[str, str],
        installation_id: int,
    ) -> tuple[tuple[CommitInfo, ...], int]:
        url: str | None = (
            f"{self._base_url}/repos/{repo_full_name}/pulls/{number}/commits"
            f"?per_page={COMMITS_PER_PAGE}"
        )
        commits: list[CommitInfo] = []
        dropped_commits = 0
        pages = 0
        while url is not None and pages < MAX_COMMIT_PAGES:
            response = await self._get(client, url, headers, "commit listing", installation_id)
            body = self._json_body(response, "commit listing")
            if not isinstance(body, list):
                raise ForgeUnavailableError(
                    PROVIDER_NAME, "commit listing returned a non-array body"
                )
            for item in body:
                mapped = _map_commit(item)
                if mapped is None:
                    dropped_commits += 1
                else:
                    commits.append(mapped)
            pages += 1
            # Only the Link header decides: no page counting, no total guessing.
            next_url = response.links.get("next", {}).get("url")
            url = (
                str(response.url.join(next_url)) if isinstance(next_url, str) and next_url else None
            )
        if url is not None:
            logger.warning(
                "commit listing hit the page cap",
                extra={
                    "provider": PROVIDER_NAME,
                    "pages": MAX_COMMIT_PAGES,
                    "commits": len(commits),
                },
            )
        return tuple(commits), dropped_commits

    async def _fetch_diff(
        self,
        client: httpx.AsyncClient,
        path: str,
        headers: dict[str, str],
        installation_id: int,
    ) -> str:
        diff_headers = {**headers, "Accept": "application/vnd.github.diff"}
        request = client.build_request("GET", f"{self._base_url}{path}", headers=diff_headers)
        response = await self._send(client, request, "diff fetch")
        try:
            _check_status(response, "diff fetch", installation_id)
            declared_length = response.headers.get("Content-Length", "")
            if declared_length.isdigit() and int(declared_length) > MAX_DIFF_BYTES:
                raise ForgeUnavailableError(PROVIDER_NAME, f"diff exceeds {MAX_DIFF_BYTES} bytes")
            received = 0
            chunks: list[bytes] = []
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > MAX_DIFF_BYTES:
                    raise ForgeUnavailableError(
                        PROVIDER_NAME, f"diff exceeds {MAX_DIFF_BYTES} bytes"
                    )
                chunks.append(chunk)
            return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
        finally:
            await response.aclose()

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        headers: dict[str, str],
        step: str,
        installation_id: int,
    ) -> dict[str, Any]:
        response = await self._get(
            client, f"{self._base_url}{path}", headers, step, installation_id
        )
        body = self._json_body(response, step)
        if not isinstance(body, dict):
            raise ForgeUnavailableError(PROVIDER_NAME, f"{step} returned a non-object body")
        return body

    async def _get(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        step: str,
        installation_id: int,
    ) -> httpx.Response:
        request = client.build_request("GET", url, headers=headers)
        response = await self._send(client, request, step)
        try:
            _check_status(response, step, installation_id)
            await response.aread()
        except Exception:
            await response.aclose()
            raise
        return response

    @staticmethod
    async def _send(client: httpx.AsyncClient, request: httpx.Request, step: str) -> httpx.Response:
        try:
            return await client.send(request, stream=True)
        except httpx.TimeoutException as error:
            raise ForgeUnavailableError(PROVIDER_NAME, f"{step} timed out") from error
        except httpx.HTTPError as error:
            raise ForgeUnavailableError(
                PROVIDER_NAME, f"{step} failed: network error ({type(error).__name__})"
            ) from error

    @staticmethod
    def _json_body(response: httpx.Response, step: str) -> Any:
        try:
            return response.json()
        except ValueError as error:
            raise ForgeUnavailableError(PROVIDER_NAME, f"{step} returned invalid JSON") from error


def _check_status(response: httpx.Response, step: str, installation_id: int) -> None:
    """Error mapping for the pull request endpoints (fetch_pull_request knows
    the installation; the installation/token endpoints in app_auth.py keep
    their own mapping). Class per docs/PIPELINE_SPEC.md §4.2: 404 is
    permanent (the PR is gone), 401/403 is permanent (access revoked) — the
    rest is retryable. Never a response body (AGENTS.md hard rule 2)."""
    if response.status_code == 404:
        raise PrNotFoundError(PROVIDER_NAME, "pull request not found")
    if response.status_code in (401, 403):
        raise InstallationGoneError(installation_id, "permission denied")
    if response.status_code >= 400:
        raise ForgeUnavailableError(PROVIDER_NAME, f"{step}: HTTP {response.status_code}")
    if response.status_code not in _PERMITTED_STATUSES:
        raise ForgeUnavailableError(
            PROVIDER_NAME, f"{step}: unexpected HTTP {response.status_code}"
        )


def _validate_repo_full_name(repo_full_name: str) -> None:
    """The name goes into a URL path, so shape is checked before any request."""
    owner, slash, name = repo_full_name.partition("/")
    if (
        not slash
        or not owner.strip()
        or not name.strip()
        or "/" in name
        or ".." in repo_full_name
        or repo_full_name.startswith("/")
    ):
        raise ForgeUnavailableError(PROVIDER_NAME, "invalid repo full name: expected owner/repo")


def _map_commit(item: Any) -> CommitInfo | None:
    if not isinstance(item, dict):
        return None
    sha = item.get("sha")
    commit = item.get("commit")
    message = commit.get("message") if isinstance(commit, dict) else None
    author = item.get("author")
    login = author.get("login") if isinstance(author, dict) else None
    try:
        return CommitInfo(
            sha=sha if isinstance(sha, str) else "",
            message=message if isinstance(message, str) else "",
            author_login=login if isinstance(login, str) else None,
        )
    except ValueError:
        return None


def _build_context(
    installation_id: int,
    repo_full_name: str,
    number: int,
    detail: dict[str, Any],
    commits: tuple[CommitInfo, ...],
    diff_text: str,
) -> PullRequestContext:
    title = detail.get("title")
    body = detail.get("body")
    head = detail.get("head")
    base = detail.get("base")
    if not isinstance(title, str) or not isinstance(head, dict) or not isinstance(base, dict):
        raise ForgeUnavailableError(PROVIDER_NAME, "pull request response was malformed")
    head_sha, head_ref = head.get("sha"), head.get("ref")
    base_sha, base_ref = base.get("sha"), base.get("ref")
    if not (
        isinstance(head_sha, str)
        and isinstance(head_ref, str)
        and isinstance(base_sha, str)
        and isinstance(base_ref, str)
    ):
        raise ForgeUnavailableError(PROVIDER_NAME, "pull request response was malformed")
    user = detail.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    user_id = user.get("id") if isinstance(user, dict) else None
    association = detail.get("author_association")
    try:
        return PullRequestContext(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            pr_number=number,
            title=title,
            description=body if isinstance(body, str) else "",
            head_sha=head_sha,
            base_sha=base_sha,
            head_ref=head_ref,
            base_ref=base_ref,
            author_login=login if isinstance(login, str) else None,
            author_external_id=user_id if isinstance(user_id, int) else None,
            author_association=association if isinstance(association, str) else None,
            commits=commits,
            diff_text=diff_text,
        )
    except ValueError as error:
        raise ForgeUnavailableError(PROVIDER_NAME, "pull request response was malformed") from error
