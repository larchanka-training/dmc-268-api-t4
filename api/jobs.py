"""Review jobs for the console: the list, one job, and its live progress.

`GET /jobs` lists the current organisation's jobs, newest first, keyset-paged.
`GET /jobs/{job_id}` returns one job with its pipeline steps and published findings, plus a
top-level `stage` — the same 4-value console stage as the SSE events below, taken from the
most recent step, for a console that only polls this endpoint and never opens the stream. A
job is visible to a session that has an organisation on the job's installation; any other job
answers 404.

`GET /jobs/{job_id}/events` streams the job's status and step changes until it reaches a
terminal status. It adds to polling of `/jobs` and does not replace it.

Wire format, one block per event:

    id: <seq>
    event: snapshot | step | status | end
    data: <json>

- `snapshot` comes first on every connection, so a client that reconnects after a gap
  redraws from it instead of replaying history; its id is the last event it covers.
- `step` and `status` follow, each with a `stage` the console's progress bar can use.
  `Last-Event-ID` is ignored on purpose: the fresh snapshot already covers everything up
  to the moment of reconnecting.
- `end` is sent once the job is terminal, and the server closes the stream.
- `: ping` comments keep proxies and the browser from dropping an idle connection.
"""

import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from api.deps import NO_STORE, ApiError, current_session, services
from domain.auth import Session
from domain.findings import PublishedFinding
from domain.jobs import (
    ACTIVE_STATUSES,
    JobEvent,
    JobQuery,
    JobSnapshot,
    StepRecord,
    decode_cursor,
    encode_cursor,
    is_terminal,
    ui_stage,
)
from domain.ports import ReviewJob, ReviewJobStatus

logger = logging.getLogger(__name__)
router = APIRouter()

HEARTBEAT_SECONDS = 15.0
# Sent once per connection: how long the browser waits before reconnecting.
RETRY_MILLISECONDS = 3000


DEFAULT_LIMIT = 25
MAX_LIMIT = 100


@router.get("/jobs")
async def list_jobs(
    request: Request,
    session: Annotated[Session, Depends(current_session)],
    status: str | None = None,
    repository: str | None = None,
    author: str | None = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: str | None = None,
    q: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    organization = session.current_organization
    if organization is None:
        return {"items": [], "next_cursor": None}
    if not 1 <= limit <= MAX_LIMIT:
        raise ApiError(400, "bad_limit")
    try:
        after = None if cursor is None else decode_cursor(cursor)
    except ValueError:
        raise ApiError(400, "bad_cursor") from None
    query = JobQuery(
        installation_ids=frozenset({organization.installation_id}),
        statuses=_statuses(status),
        repository=repository,
        author=author,
        created_from=_instant(from_),
        created_to=_instant(to),
        text=q or None,
        after=after,
        limit=limit,
    )
    page = await services(request).jobs.list_page(query)
    return {
        "items": [_job_json(job) for job in page.items],
        "next_cursor": None if page.next_after is None else encode_cursor(page.next_after),
    }


@router.get("/jobs/{job_id}")
async def read_job(
    job_id: str,
    request: Request,
    session: Annotated[Session, Depends(current_session)],
) -> dict[str, Any]:
    jobs = services(request).jobs
    job = await jobs.get(job_id)
    visible = {organization.installation_id for organization in session.organizations}
    if job is None or job.payload.installation_id not in visible:
        raise ApiError(404, "not_found")
    steps = await jobs.steps(job_id)
    findings = await services(request).findings.for_job(job_id)
    pull_request_url = _pull_request_url(job)
    return {
        **_job_json(job),
        # The console's 4-stage progress bar; null before the first step is recorded.
        "stage": _current_stage(steps),
        # Not stored yet: the summary, the diagnostic log and the check run link.
        "summary": None,
        "error_log": None,
        "external_check_url": None,
        "steps": [_step_json(step) for step in steps],
        "findings": [_finding_json(finding, pull_request_url) for finding in findings],
    }


@router.get("/jobs/{job_id}/events")
async def stream_job_events(
    job_id: str,
    request: Request,
    session: Annotated[Session, Depends(current_session)],
) -> StreamingResponse:
    source = services(request).job_events
    snapshot = await source.snapshot(job_id)
    visible = {organization.installation_id for organization in session.organizations}
    if snapshot is None or snapshot.installation_id not in visible:
        # Someone else's job answers exactly like a missing one: nothing to enumerate.
        raise ApiError(404, "not_found")

    return StreamingResponse(
        _events(request, snapshot),
        media_type="text/event-stream",
        headers={
            "Cache-Control": NO_STORE,
            # Harmless behind Caddy, which flushes event streams on its own; nginx
            # would buffer the stream without it.
            "X-Accel-Buffering": "no",
        },
    )


async def _events(request: Request, snapshot: JobSnapshot) -> AsyncIterator[str]:
    source = services(request).job_events
    yield f"retry: {RETRY_MILLISECONDS}\n\n"
    yield _block("snapshot", snapshot.last_seq, _snapshot_json(snapshot))
    if is_terminal(snapshot.status):
        yield _block("end", snapshot.last_seq, {"status": str(snapshot.status)})
        return

    last_seq = snapshot.last_seq
    while not await request.is_disconnected():
        events = await source.next_events(snapshot.job_id, last_seq, HEARTBEAT_SECONDS)
        if not events:
            yield ": ping\n\n"
            continue
        for event in events:
            last_seq = event.seq
            yield _block("status" if event.step is None else "step", event.seq, _event_json(event))
            if is_terminal(event.status):
                yield _block("end", event.seq, {"status": str(event.status)})
                return


def _block(kind: str, seq: int, data: dict[str, Any]) -> str:
    return f"id: {seq}\nevent: {kind}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _snapshot_json(snapshot: JobSnapshot) -> dict[str, Any]:
    return {
        "job_id": snapshot.job_id,
        "status": str(snapshot.status),
        "seq": snapshot.last_seq,
        "steps": [_event_json(step) for step in snapshot.steps],
    }


def _event_json(event: JobEvent) -> dict[str, Any]:
    return {
        "job_id": event.job_id,
        "seq": event.seq,
        "at": event.at.isoformat(),
        "status": str(event.status),
        "step": None if event.step is None else str(event.step),
        "step_status": None if event.step_status is None else str(event.step_status),
        "stage": None if event.step is None else str(ui_stage(event.step)),
        "attempt": event.attempt,
        "error_kind": event.error_kind,
    }


def _statuses(raw: str | None) -> frozenset[ReviewJobStatus]:
    if raw is None:
        return frozenset()
    if raw == "active":
        return ACTIVE_STATUSES
    try:
        return frozenset({ReviewJobStatus(raw)})
    except ValueError:
        raise ApiError(400, "bad_status") from None


def _instant(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise ApiError(400, "bad_date") from None
    if parsed.tzinfo is None:
        raise ApiError(400, "bad_date")
    return parsed


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _job_json(job: ReviewJob) -> dict[str, Any]:
    payload = job.payload
    return {
        "id": job.job_id,
        "pull_request": {
            "provider": payload.provider,
            "repo_full_name": payload.repo_full_name,
            "number": payload.pr_number,
            "title": payload.pr_title,
            "author": payload.author_login or "",
            "head_ref": payload.head_ref or "",
            "base_ref": payload.base_ref or "",
            "head_sha": payload.head_sha or "",
            "url": _pull_request_url(job),
        },
        "status": str(job.status),
        "trigger": "command" if payload.event_action == "command" else "webhook",
        "created_at": job.created_at.isoformat(),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        "finding_count": 0 if job.stats is None else job.stats.findings_published,
        "error_kind": job.error_kind,
        "retry_count": job.retry_count,
        "next_attempt_at": _iso(job.next_attempt_at),
    }


def _pull_request_url(job: ReviewJob) -> str:
    payload = job.payload
    return f"https://github.com/{payload.repo_full_name}/pull/{payload.pr_number}"


def _finding_json(finding: PublishedFinding, pull_request_url: str) -> dict[str, Any]:
    suggestion = finding.suggestion
    return {
        "id": str(uuid.UUID(finding.id)),
        "path": finding.path,
        "line": finding.line,
        "start_line": finding.start_line,
        "side": str(finding.side),
        "severity": str(finding.severity),
        "category": str(finding.category),
        "message": finding.message,
        "suggestion": None
        if suggestion is None
        else {"before": list(suggestion.before), "after": list(suggestion.after)},
        "published_at": finding.posted_at.isoformat(),
        "external_url": _finding_url(finding, pull_request_url),
    }


def _finding_url(finding: PublishedFinding, pull_request_url: str) -> str:
    """The inline comment, else the review whose body carries the finding (D5)."""
    if finding.external_comment_id is not None:
        return f"{pull_request_url}#discussion_r{finding.external_comment_id}"
    if finding.external_review_id is not None:
        return f"{pull_request_url}#pullrequestreview-{finding.external_review_id}"
    return pull_request_url


def _step_json(step: StepRecord) -> dict[str, Any]:
    return {
        "step": str(step.step),
        "status": str(step.status),
        "stage": str(ui_stage(step.step)),
        "attempt": step.attempt,
        "started_at": step.started_at.isoformat(),
        "finished_at": _iso(step.finished_at),
        "error_kind": step.error_kind,
        "metrics": {},
    }


def _current_stage(steps: Sequence[StepRecord]) -> str | None:
    """The stage the console's progress bar should highlight: the most recent step's.

    `jobs.steps` orders by `(attempt, seq)` (adapters/db/repository.py), so the last
    element is the most recent step of the most recent attempt. `None` before the first
    step is recorded, which is the same "not started yet" the SSE snapshot shows.
    """
    return None if not steps else str(ui_stage(steps[-1].step))
