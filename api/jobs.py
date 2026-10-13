"""Live progress of a review job, as Server-Sent Events.

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
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from api.deps import NO_STORE, ApiError, current_session, services
from domain.auth import Session
from domain.jobs import JobEvent, JobSnapshot, is_terminal, ui_stage

logger = logging.getLogger(__name__)
router = APIRouter()

HEARTBEAT_SECONDS = 15.0
# Sent once per connection: how long the browser waits before reconnecting.
RETRY_MILLISECONDS = 3000


@router.get("/jobs/{job_id}/events")
async def stream_job_events(
    job_id: str,
    request: Request,
    session: Annotated[Session, Depends(current_session)],
) -> StreamingResponse:
    source = services(request).job_events
    snapshot = await source.snapshot(job_id)
    visible = {organization.id for organization in session.organizations}
    if snapshot is None or snapshot.account_id not in visible:
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
