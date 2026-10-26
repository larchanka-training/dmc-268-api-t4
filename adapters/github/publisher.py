"""Posting a planned review on a GitHub pull request, as the App installation.

Two outputs, per WORKFLOW_DESIGN.md §5:

1. **A check run** carrying the summary and the conclusion. Created on the head SHA, or
   updated in place when an earlier job left one on the same SHA.
2. **One pull request review** holding every new inline comment, sent in a single
   request. One request rather than one per comment keeps the bot inside GitHub's
   secondary limits on content creation, and the review is skipped entirely when there
   is nothing new to say (`PublishPlan.needs_review`).

If GitHub refuses the inline comments (422: a line it will not anchor to, usually a
hunk boundary the plan's diff did not match), the review is sent again without them and
the comments go into its body, so the findings still reach the pull request.

The App needs `Pull requests: Read and write` and `Checks: Read and write`, accepted by
the installation; without them GitHub answers 403 and this raises `ForgePermissionError`.
"""

import asyncio
import logging
from typing import Any

import httpx

from adapters.github.app_auth import GitHubAppAuth
from adapters.github.config import PROVIDER_NAME, GitHubAppSettings
from domain.errors import (
    ForgePermissionError,
    ForgeRateLimitedError,
    ForgeUnavailableError,
    PrNotFoundError,
)
from domain.models import DiffSide
from domain.ports import (
    ExistingCheckRun,
    PublishedComment,
    PublishOutcome,
    PullRequestTarget,
)
from domain.publishing import PlannedComment, PublishPlan, fingerprint_in

logger = logging.getLogger(__name__)

CHECK_RUN_NAME = "AI review"
_CHECK_TITLE = {
    "success": "No issues found",
    "neutral": "Issues worth a look",
    "failure": "Changes requested",
    # The only skip today is a pull request over the LLM input budget (issue #55).
    "skipped": "Skipped: pull request too large",
}
_SIDE = {DiffSide.OLD: "LEFT", DiffSide.NEW: "RIGHT"}
_REVIEW_COMMENTS_PER_PAGE = 100


class GitHubReviewPublisher:
    """Implements domain.ports.ReviewPublisher against github.com."""

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
        # Comment posts are serialised per repository (WORKFLOW_DESIGN.md §9), within
        # this process.
        self._locks: dict[tuple[int, str, str], asyncio.Lock] = {}

    async def publish(
        self,
        target: PullRequestTarget,
        plan: PublishPlan,
        existing_check: ExistingCheckRun | None = None,
    ) -> PublishOutcome:
        key = (target.installation_id, target.owner, target.repo)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            token = await self._app_auth.installation_token(target.installation_id)
            async with httpx.AsyncClient(
                base_url=self._settings.api_base_url,
                timeout=self._settings.timeout_seconds,
                transport=self._transport,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            ) as client:
                check = await self._upsert_check_run(client, target, plan, existing_check)
                review_id = review_url = None
                comments: tuple[PublishedComment, ...] = ()
                fallback = False
                if plan.needs_review:
                    review, fallback = await self._create_review(client, target, plan)
                    review_id = _int(review.get("id"))
                    review_url = _str(review.get("html_url"))
                    comments = await self._published_comments(
                        client, target, plan, review_id, review_url, fallback
                    )

        logger.info(
            "review published",
            extra={
                "provider": PROVIDER_NAME,
                "installation_id": target.installation_id,
                "comments": len(comments),
                "inline_fallback": fallback,
                "conclusion": str(plan.conclusion),
            },
        )
        return PublishOutcome(
            check_run_id=check[0],
            check_run_url=check[1],
            review_id=review_id,
            review_url=review_url,
            comments=comments,
            inline_fallback=fallback,
        )

    async def find_check_run(self, target: PullRequestTarget) -> ExistingCheckRun | None:
        """The newest check run of this App named CHECK_RUN_NAME on the head SHA."""
        token = await self._app_auth.installation_token(target.installation_id)
        async with httpx.AsyncClient(
            base_url=self._settings.api_base_url,
            timeout=self._settings.timeout_seconds,
            transport=self._transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        ) as client:
            # Another App may name its check the same; PATCHing that one answers 403.
            params = {
                "check_name": CHECK_RUN_NAME,
                "filter": "latest",
                "app_id": self._settings.app_id,
            }
            response = await self._send(
                client,
                "GET",
                f"/repos/{target.owner}/{target.repo}/commits/{target.head_sha}/check-runs",
                params=params,
            )
        runs = _object(response).get("check_runs")
        for run in runs if isinstance(runs, list) else []:
            check_id = _int(run.get("id")) if isinstance(run, dict) else None
            if check_id is not None:
                return ExistingCheckRun(id=check_id, head_sha=target.head_sha)
        return None

    async def _upsert_check_run(
        self,
        client: httpx.AsyncClient,
        target: PullRequestTarget,
        plan: PublishPlan,
        existing: ExistingCheckRun | None,
    ) -> tuple[int, str | None]:
        output = {
            "title": _CHECK_TITLE[str(plan.conclusion)],
            "summary": plan.summary,
        }
        base = f"/repos/{target.owner}/{target.repo}/check-runs"
        if existing is not None and existing.head_sha == target.head_sha:
            body: dict[str, Any] = {
                "status": "completed",
                "conclusion": str(plan.conclusion),
                "output": output,
            }
            response = await self._send(client, "PATCH", f"{base}/{existing.id}", body)
        else:
            body = {
                "name": CHECK_RUN_NAME,
                "head_sha": target.head_sha,
                "status": "completed",
                "conclusion": str(plan.conclusion),
                "output": output,
            }
            response = await self._send(client, "POST", base, body)
        payload = _object(response)
        check_id = _int(payload.get("id"))
        if check_id is None:
            raise ForgeUnavailableError(PROVIDER_NAME, "check run response carried no id")
        return check_id, _str(payload.get("html_url"))

    async def _create_review(
        self, client: httpx.AsyncClient, target: PullRequestTarget, plan: PublishPlan
    ) -> tuple[dict[str, Any], bool]:
        path = f"/repos/{target.owner}/{target.repo}/pulls/{target.number}/reviews"
        body: dict[str, Any] = {
            "commit_id": target.head_sha,
            "event": str(plan.review_event),
            "body": _review_body(plan),
            "comments": [_comment_json(comment) for comment in plan.comments],
        }
        try:
            response = await self._send(client, "POST", path, body)
            return _object(response), False
        except _Unprocessable:
            if not plan.comments:
                raise ForgeUnavailableError(PROVIDER_NAME, "review rejected with 422") from None
            logger.warning(
                "inline comments rejected; posting them in the review body",
                extra={"provider": PROVIDER_NAME, "comments": len(plan.comments)},
            )
        body["comments"] = []
        body["body"] = _review_body(plan, inline=plan.comments)
        response = await self._send(client, "POST", path, body)
        return _object(response), True

    async def _published_comments(
        self,
        client: httpx.AsyncClient,
        target: PullRequestTarget,
        plan: PublishPlan,
        review_id: int | None,
        review_url: str | None,
        fallback: bool,
    ) -> tuple[PublishedComment, ...]:
        """The id and URL of each comment, matched to its finding by the fingerprint marker.

        Creating a review does not return its comments, so they are listed once after.
        """
        if fallback or review_id is None or not plan.comments:
            return tuple(
                PublishedComment(comment.fingerprint, None, review_url) for comment in plan.comments
            )
        path = (
            f"/repos/{target.owner}/{target.repo}/pulls/{target.number}"
            f"/reviews/{review_id}/comments"
        )
        response = await self._send(
            client, "GET", path, params={"per_page": _REVIEW_COMMENTS_PER_PAGE}
        )
        listed = response.json() if response.content else []
        found: dict[str, tuple[int | None, str | None]] = {}
        if isinstance(listed, list):
            for item in listed:
                if isinstance(item, dict) and isinstance(item.get("body"), str):
                    digest = fingerprint_in(item["body"])
                    if digest is not None:
                        found[digest] = (_int(item.get("id")), _str(item.get("html_url")))
        return tuple(
            PublishedComment(
                comment.fingerprint, *found.get(comment.fingerprint, (None, review_url))
            )
            for comment in plan.comments
        )

    async def _send(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = await client.request(method, path, json=body, params=params)
        except httpx.TimeoutException as error:
            raise ForgeUnavailableError(PROVIDER_NAME, f"{method} timed out") from error
        except httpx.HTTPError as error:
            raise ForgeUnavailableError(
                PROVIDER_NAME, f"{method} failed: {type(error).__name__}"
            ) from error
        _raise_for_status(response, method)
        return response


class _Unprocessable(Exception):
    """GitHub answered 422. Internal: the caller decides whether a fallback exists."""


def _raise_for_status(response: httpx.Response, method: str) -> None:
    status = response.status_code
    if status < 400:
        return
    message = _message(response)
    if status == 429 or (status == 403 and _is_rate_limit(response, message)):
        raise ForgeRateLimitedError(PROVIDER_NAME, _retry_after(response))
    if status == 403:
        # Message text from GitHub, never our request body: it carries no secret.
        raise ForgePermissionError(PROVIDER_NAME, f"{method} returned 403: {message[:200]}")
    if status in (404, 410):
        raise PrNotFoundError(PROVIDER_NAME, f"{method} returned {status}")
    if status == 422:
        raise _Unprocessable()
    raise ForgeUnavailableError(PROVIDER_NAME, f"{method} returned HTTP {status}")


def _is_rate_limit(response: httpx.Response, message: str) -> bool:
    return (
        response.headers.get("x-ratelimit-remaining") == "0"
        or "retry-after" in response.headers
        or "rate limit" in message.lower()
    )


def _retry_after(response: httpx.Response) -> int | None:
    raw = response.headers.get("retry-after")
    if raw is not None and raw.strip().isdigit():
        return int(raw.strip())
    return None


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        return str(body["message"])
    return ""


def _comment_json(comment: PlannedComment) -> dict[str, Any]:
    position = comment.finding.position
    item: dict[str, Any] = {
        "path": position.path,
        "line": position.line,
        "side": _SIDE[position.side],
        "body": comment.body,
    }
    if comment.start_line is not None:
        item["start_line"] = comment.start_line
        item["start_side"] = _SIDE[position.side]
    return item


def _review_body(plan: PublishPlan, inline: tuple[PlannedComment, ...] = ()) -> str:
    """Short: the full summary lives in the check run, updated per head SHA."""
    counts = ", ".join(
        f"{count} {severity}" for severity, count in sorted(plan.stats.by_severity.items())
    )
    parts = [
        f"**AI review:** {plan.verdict.value.replace('_', ' ')}"
        + (f" ({counts})" if counts else "")
    ]
    parts.append("Details are in the **AI review** check on this commit.")
    for comment in inline:
        position = comment.finding.position
        parts.extend(["", "---", f"`{position.path}:{position.line}`", "", comment.body])
    return "\n".join(parts)


def _object(response: httpx.Response) -> dict[str, Any]:
    body = response.json() if response.content else {}
    if not isinstance(body, dict):
        raise ForgeUnavailableError(PROVIDER_NAME, "response was not an object")
    return body


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None
