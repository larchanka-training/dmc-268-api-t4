"""The GitHub webhook endpoint: Steps 2 and 3 of the review workflow.

Implements [docs/WORKFLOW_DESIGN.md §2](../../docs/WORKFLOW_DESIGN.md): read
the raw body capped at 5 MB, verify the HMAC signature over exactly the
received bytes, route on the event header, then enqueue one review job per
reviewable `pull_request` action and start the processing task
(tasks/plan.md decision 2, 2026-10-07). The route is forge-to-server: no
session, no CSRF headers — the delivery signature is the authentication.

Every verified delivery logs its routing decision — event, action, delivery
id, repo, PR, installation, short SHAs — so a real GitHub delivery is
traceable in the console. Delivery content beyond those routing fields is
never logged (AGENTS.md hard rule 3).
"""

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from adapters.github.config import PROVIDER_NAME
from adapters.github.payload import parse_pull_request_event, reviewable_action
from adapters.github.signature import SIGNATURE_HEADER, verify_webhook_signature
from api.deps import ApiError
from domain.errors import WebhookPayloadError, WebhookSignatureError
from domain.ports import GitProvider, JobRepository, NewReviewJob
from worker.process import process_review_job

logger = logging.getLogger("github_event")
router = APIRouter()

#: docs/WORKFLOW_DESIGN.md §2 Step 2 point 1: the raw body is capped at 5 MB
#: before any JSON parsing.
MAX_BODY = 5 * 1024 * 1024

EVENT_HEADER = "x-github-event"
DELIVERY_HEADER = "x-github-delivery"
PULL_REQUEST_EVENT = "pull_request"

#: In-flight processing tasks. A bare task result can be garbage-collected
#: mid-run, so the module keeps a reference until the task finishes
#: (finished tasks discard themselves). Tests drain this set instead of
#: sleeping.
BACKGROUND_TASKS: set[asyncio.Future[None]] = set()


@dataclass(frozen=True, slots=True)
class GitHubWebhookDeps:
    """What the webhook route needs, wired once by the app factory."""

    jobs: JobRepository
    vcs: GitProvider
    # A secret: out of the repr, so a logged dataclass cannot leak it.
    webhook_secret: str = field(repr=False)


def webhook_deps(request: Request) -> GitHubWebhookDeps:
    return cast(GitHubWebhookDeps, request.app.state.webhooks)


def get_action(payload: Mapping[str, Any]) -> str:
    """The action for one routing log line: routing metadata only, clipped
    hard, so an adversarial payload cannot smuggle content into the logs."""
    action = payload.get("action")
    if isinstance(action, str) and action:
        return action[:40]
    return "<missing>"


async def drain_background_tasks() -> None:
    """Await every in-flight processing task.

    Tests call this to observe a job's final state deterministically;
    production does not (the worker claim loop of Step 4 replaces the
    direct dispatch later).
    """
    while BACKGROUND_TASKS:
        await asyncio.gather(*BACKGROUND_TASKS)


async def _process_review(deps: GitHubWebhookDeps, job_id: str, job: NewReviewJob) -> None:
    """Wrap one job run so the tracked tasks are always Task[None]."""
    await process_review_job(job_id, job, deps.vcs, deps.jobs)


@router.post("/webhooks/github")
async def receive_github_webhook(request: Request) -> Response:
    deps = webhook_deps(request)

    # Step 2 point 1: the raw bytes, capped before parsing. A declared
    # Content-Length over the cap is rejected before the body is read at
    # all; the post-read check below is the backstop for chunked or
    # absent Content-Length. The signature is verified over exactly the
    # received bytes — re-serialized JSON would not match.
    content_length = request.headers.get("Content-Length")
    if content_length is not None and content_length.isdigit() and int(content_length) > MAX_BODY:
        raise ApiError(413, "payload_too_large")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ApiError(413, "payload_too_large")

    try:
        verify_webhook_signature(raw, request.headers.get(SIGNATURE_HEADER), deps.webhook_secret)
    except WebhookSignatureError:
        # Step 2 point 3: a forged request is not a successful one — 401,
        # never 200.
        raise ApiError(401, "invalid_signature") from None

    try:
        payload = json.loads(raw)
    except ValueError:
        raise ApiError(400, "invalid_json") from None
    if not isinstance(payload, dict):
        raise ApiError(400, "invalid_payload")

    # Step 2 point 4: route on the event header before the action. Only
    # `pull_request` goes to the review path; ping, lifecycle events and
    # anything unknown get 204 so GitHub stops redelivering them.
    gh_event = request.headers.get(EVENT_HEADER)
    if gh_event != PULL_REQUEST_EVENT:
        logger.info("github webhook ignored: event=%s action=%s", gh_event, get_action(payload))
        return Response(status_code=204)

    if reviewable_action(payload) is None:
        # Step 2 point 5: a non-reviewable action is not an error; it simply
        # does not review.
        logger.info("github webhook ignored: event=pull_request action=%s", get_action(payload))
        return Response(status_code=204)

    try:
        event = parse_pull_request_event(payload)
    except WebhookPayloadError as error:
        # The reason names fields only, never payload content, so it is safe
        # to log; the response keeps the fixed ApiError wire shape.
        logger.warning("github webhook payload rejected: %s", error.reason)
        raise ApiError(400, "invalid_payload") from None

    logger.info(
        "github webhook accepted: event=pull_request action=%s repo=%s "
        "pr=%s installation=%s head=%s base=%s",
        event.action.value,
        event.repo_full_name,
        event.pr_number,
        event.installation_id,
        event.head_sha[:7],
        event.base_sha[:7],
    )

    try:
        job = NewReviewJob(
            provider=PROVIDER_NAME,
            delivery_id=request.headers.get(DELIVERY_HEADER),
            installation_id=event.installation_id,
            repo_id=event.repo_id,
            repo_full_name=event.repo_full_name,
            pr_number=event.pr_number,
            head_sha=event.head_sha,
            base_sha=event.base_sha,
            event_action=event.action.value,
        )
    except ValueError:
        raise ApiError(400, "invalid_payload") from None

    # Step 3: one enqueue, idempotent on (provider, delivery_id) — a
    # redelivered request must still see success, just no second job.
    job_id = await deps.jobs.enqueue(job)
    if job_id is None:
        logger.info(
            "github webhook duplicate delivery: action=%s repo=%s pr=%s",
            event.action.value,
            event.repo_full_name,
            event.pr_number,
        )
        return JSONResponse({"status": "duplicate"}, status_code=202)

    task = asyncio.create_task(_process_review(deps, job_id, job))
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)
    return JSONResponse({"job_id": job_id, "status": "queued"}, status_code=202)
