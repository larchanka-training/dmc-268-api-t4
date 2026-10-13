"""Publishing a review against recorded GitHub responses. No test opens a connection."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from adapters.github.app_auth import GitHubAppAuth
from adapters.github.config import GitHubAppSettings
from adapters.github.publisher import CHECK_RUN_NAME, GitHubReviewPublisher
from domain.errors import (
    ForgePermissionError,
    ForgeRateLimitedError,
    ForgeUnavailableError,
    PrNotFoundError,
)
from domain.models import Category, DiffSide, Finding, FindingPosition, Severity
from domain.ports import ExistingCheckRun, PullRequestTarget
from domain.publishing import CommentableLines, PublishPlan, build_plan
from tests.conftest import FixedClock, RecordingTransport, github_fixture

KEY_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "github" / "app_key.pem"
HEAD = "c0ffee0000000000000000000000000000000000"
TARGET = PullRequestTarget(installation_id=42, owner="acme", repo="web", number=7, head_sha=HEAD)
PATCH = "@@ -1,2 +1,3 @@\n import os\n+password = 'hunter2'\n print(os.name)"

Handler = Callable[[httpx.Request], httpx.Response]


def settings() -> GitHubAppSettings:
    return GitHubAppSettings(
        app_id="123456",
        private_key=KEY_PATH.read_text(encoding="utf-8"),
        app_slug="review-agent",
        api_base_url="https://api.github.test",
        webhook_secret="whsec_test",
    )


def plan_with(*lines_: int) -> PublishPlan:
    findings = [
        Finding(
            position=FindingPosition(path="app.py", line=line, side=DiffSide.NEW),
            severity=Severity.HIGH,
            category=Category.SECURITY,
            message=f"Hard-coded credential on line {line}.",
            suggestion=None,
        )
        for line in lines_
    ]
    return build_plan(
        findings,
        model_summary="Adds a config line.",
        lines=CommentableLines.from_patches({"app.py": PATCH}),
    )


def github(
    *,
    review: Handler | None = None,
    check: Handler | None = None,
    listed: Handler | None = None,
) -> Handler:
    """One handler for every endpoint the publisher calls, each overridable."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/access_tokens"):
            return httpx.Response(200, json=github_fixture("installation_token"))
        if "/check-runs" in path:
            if check is not None:
                return check(request)
            return httpx.Response(
                201 if request.method == "POST" else 200,
                json={"id": 555, "html_url": "https://github.com/acme/web/runs/555"},
            )
        if path.endswith("/comments"):
            if listed is not None:
                return listed(request)
            return httpx.Response(200, json=[])
        if path.endswith("/reviews"):
            if review is not None:
                return review(request)
            return httpx.Response(
                200, json={"id": 900, "html_url": "https://github.com/acme/web/pull/7#r900"}
            )
        return httpx.Response(404, json={"message": "Not Found"})

    return handler


def publisher_for(
    clock: FixedClock, handler: Handler
) -> tuple[GitHubReviewPublisher, RecordingTransport]:
    transport = RecordingTransport(handler)
    config = settings()
    return GitHubReviewPublisher(
        config, GitHubAppAuth(config, clock, transport=transport), transport=transport
    ), transport


def sent(transport: RecordingTransport, method: str, suffix: str) -> list[dict[str, Any]]:
    return [
        json.loads(request.content)
        for request in transport.requests
        if request.method == method and request.url.path.endswith(suffix)
    ]


async def test_new_comments_go_in_one_review_and_the_summary_in_a_check_run(
    auth_clock: FixedClock,
) -> None:
    plan = plan_with(2)
    marker_body = plan.comments[0].body

    def listed(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[{"id": 31, "html_url": "https://github.com/c/31", "body": marker_body}]
        )

    publisher, transport = publisher_for(auth_clock, github(listed=listed))

    outcome = await publisher.publish(TARGET, plan)

    (check,) = sent(transport, "POST", "/check-runs")
    assert check["name"] == CHECK_RUN_NAME
    assert check["head_sha"] == HEAD
    assert check["conclusion"] == "neutral"
    assert check["output"]["summary"] == plan.summary

    (review,) = sent(transport, "POST", "/reviews")
    assert review["commit_id"] == HEAD
    assert review["event"] == "COMMENT"
    assert review["comments"] == [
        {"path": "app.py", "line": 2, "side": "RIGHT", "body": marker_body}
    ]

    assert outcome.check_run_id == 555
    assert outcome.review_id == 900
    assert outcome.inline_fallback is False
    (comment,) = outcome.comments
    assert comment.fingerprint == plan.comments[0].fingerprint
    assert comment.external_comment_id == 31
    assert comment.url == "https://github.com/c/31"
    for request in transport.requests:
        if not request.url.path.endswith("/access_tokens"):
            assert request.headers["authorization"] == "Bearer ghs_test_aaaaaaaaaaaaaaaaaaaaaaaa"


async def test_the_check_run_is_updated_in_place_on_the_same_head(auth_clock: FixedClock) -> None:
    publisher, transport = publisher_for(auth_clock, github())

    await publisher.publish(TARGET, plan_with(), ExistingCheckRun(id=555, head_sha=HEAD))

    assert sent(transport, "POST", "/check-runs") == []
    (update,) = sent(transport, "PATCH", "/check-runs/555")
    assert update["conclusion"] == "success"
    assert "head_sha" not in update


async def test_a_push_creates_a_new_check_run_on_the_new_head(auth_clock: FixedClock) -> None:
    publisher, transport = publisher_for(auth_clock, github())

    await publisher.publish(
        TARGET, plan_with(), ExistingCheckRun(id=555, head_sha="old" * 13 + "o")
    )

    assert len(sent(transport, "POST", "/check-runs")) == 1
    assert sent(transport, "PATCH", "/check-runs/555") == []


async def test_nothing_new_posts_no_review(auth_clock: FixedClock) -> None:
    publisher, transport = publisher_for(auth_clock, github())

    outcome = await publisher.publish(TARGET, plan_with())

    assert sent(transport, "POST", "/reviews") == []
    assert outcome.review_id is None
    assert outcome.comments == ()


async def test_rejected_inline_comments_fall_back_to_the_review_body(
    auth_clock: FixedClock,
) -> None:
    attempts: list[dict[str, Any]] = []

    def review(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        attempts.append(body)
        if body["comments"]:
            return httpx.Response(422, json={"message": "Line could not be resolved"})
        return httpx.Response(200, json={"id": 901, "html_url": "https://github.com/r/901"})

    plan = plan_with(2)
    publisher, _ = publisher_for(auth_clock, github(review=review))

    outcome = await publisher.publish(TARGET, plan)

    assert len(attempts) == 2
    assert attempts[1]["comments"] == []
    assert plan.comments[0].body in attempts[1]["body"]
    assert outcome.inline_fallback is True
    assert outcome.comments[0].external_comment_id is None
    assert outcome.comments[0].url == "https://github.com/r/901"


async def test_a_missing_permission_is_reported_as_such(auth_clock: FixedClock) -> None:
    def check(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Resource not accessible by integration"})

    publisher, _ = publisher_for(auth_clock, github(check=check))

    with pytest.raises(ForgePermissionError, match="not accessible by integration"):
        await publisher.publish(TARGET, plan_with(2))


@pytest.mark.parametrize(
    ("response", "retry_after"),
    [
        (httpx.Response(429, headers={"Retry-After": "30"}, json={}), 30),
        (
            httpx.Response(403, json={"message": "You have exceeded a secondary rate limit."}),
            None,
        ),
    ],
)
async def test_rate_limits_are_retryable_and_carry_retry_after(
    auth_clock: FixedClock, response: httpx.Response, retry_after: int | None
) -> None:
    publisher, _ = publisher_for(auth_clock, github(review=lambda _: response))

    with pytest.raises(ForgeRateLimitedError) as raised:
        await publisher.publish(TARGET, plan_with(2))

    assert raised.value.retry_after_seconds == retry_after
    assert isinstance(raised.value, ForgeUnavailableError)


async def test_a_deleted_pull_request_is_permanent(auth_clock: FixedClock) -> None:
    publisher, _ = publisher_for(
        auth_clock, github(review=lambda _: httpx.Response(404, json={"message": "Not Found"}))
    )

    with pytest.raises(PrNotFoundError):
        await publisher.publish(TARGET, plan_with(2))


async def test_a_server_error_is_retryable(auth_clock: FixedClock) -> None:
    publisher, _ = publisher_for(
        auth_clock, github(check=lambda _: httpx.Response(502, text="bad gateway"))
    )

    with pytest.raises(ForgeUnavailableError, match="HTTP 502"):
        await publisher.publish(TARGET, plan_with(2))
