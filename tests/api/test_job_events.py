"""The job progress stream over TestClient, with the in-memory events source."""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

import api.jobs
from adapters.memory.job_events import InMemoryJobEventsSource
from domain.jobs import JobEvent, JobStep, StepStatus, UiStage, ui_stage
from domain.ports import Profile, ReviewJobStatus
from tests.api.test_repositories import sign_in
from tests.conftest import (
    TEST_ORIGIN,
    FakeIdentityProvider,
    FixedClock,
    build_test_app,
    make_organization,
    make_user,
)

ORG = make_organization("acme", "100", installation_id=42)
AT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def event(
    seq: int,
    status: ReviewJobStatus = ReviewJobStatus.PROCESSING,
    step: JobStep | None = None,
    step_status: StepStatus | None = None,
) -> JobEvent:
    return JobEvent(
        seq=seq, job_id="job-1", at=AT, status=status, step=step, step_status=step_status
    )


def client_for(clock: FixedClock, source: InMemoryJobEventsSource) -> TestClient:
    identity = FakeIdentityProvider(Profile(user=make_user(), organizations=(ORG,)))
    client = TestClient(build_test_app(clock, identity, job_events=source), base_url=TEST_ORIGIN)
    sign_in(client)
    return client


def read_stream(client: TestClient, job_id: str = "job-1") -> list[tuple[str, dict[str, Any]]]:
    blocks: list[tuple[str, dict[str, Any]]] = []
    with client.stream("GET", f"/jobs/{job_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        text = "".join(response.iter_text())
    for raw in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in raw.splitlines() if ": " in line)
        if "event" in fields:
            blocks.append((fields["event"], json.loads(fields["data"])))
    return blocks


def test_a_running_job_streams_snapshot_then_steps_until_it_ends(auth_clock: FixedClock) -> None:
    source = InMemoryJobEventsSource()
    source.create("job-1", installation_id=ORG.installation_id)
    source.record_nowait(event(1, step=JobStep.CLAIMED, step_status=StepStatus.OK))
    client = client_for(auth_clock, source)
    # Recorded before the stream opens, after the snapshot is taken — exactly what the
    # stream must deliver as events rather than lose. The terminal one ends it.
    original = source.snapshot

    async def snapshot_then_progress(job_id: str) -> Any:
        taken = await original(job_id)
        if taken is not None and taken.last_seq == 1:
            source.record_nowait(event(2, step=JobStep.LLM_CALL, step_status=StepStatus.RUNNING))
            source.record_nowait(event(3, status=ReviewJobStatus.COMPLETED))
        return taken

    source.snapshot = snapshot_then_progress  # type: ignore[method-assign]

    blocks = read_stream(client)

    kinds = [kind for kind, _ in blocks]
    assert kinds == ["snapshot", "step", "status", "end"]
    snapshot = blocks[0][1]
    assert snapshot["status"] == "PROCESSING"
    assert [step["step"] for step in snapshot["steps"]] == ["CLAIMED"]
    step = blocks[1][1]
    assert step["step"] == "LLM_CALL"
    assert step["step_status"] == "RUNNING"
    assert step["stage"] == "reviewing"
    assert blocks[3][1] == {"status": "COMPLETED"}


def test_a_finished_job_sends_its_snapshot_and_ends(auth_clock: FixedClock) -> None:
    source = InMemoryJobEventsSource()
    source.create("job-1", installation_id=ORG.installation_id)
    source.record_nowait(event(1, status=ReviewJobStatus.FAILED))

    blocks = read_stream(client_for(auth_clock, source))

    assert [kind for kind, _ in blocks] == ["snapshot", "end"]
    assert blocks[1][1] == {"status": "FAILED"}


def test_another_accounts_job_is_not_found(auth_clock: FixedClock) -> None:
    source = InMemoryJobEventsSource()
    source.create("job-1", installation_id=999)
    client = client_for(auth_clock, source)

    assert client.get("/jobs/job-1/events").status_code == 404
    assert client.get("/jobs/nope/events").json() == {"error": "not_found"}


def test_the_stream_needs_a_session(auth_clock: FixedClock) -> None:
    source = InMemoryJobEventsSource()
    source.create("job-1", installation_id=ORG.installation_id)
    identity = FakeIdentityProvider()
    client = TestClient(
        build_test_app(auth_clock, identity, job_events=source), base_url=TEST_ORIGIN
    )

    assert client.get("/jobs/job-1/events").status_code == 401


class _ConnectedRequest:
    """Just enough of a Request for the event generator."""

    def __init__(self, source: InMemoryJobEventsSource) -> None:
        self.app = type("App", (), {})()
        self.app.state = type("State", (), {})()
        self.app.state.services = type("Services", (), {"job_events": source})()

    async def is_disconnected(self) -> bool:
        return False


async def test_an_idle_stream_sends_heartbeats_and_wakes_on_new_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api.jobs, "HEARTBEAT_SECONDS", 0.05)
    source = InMemoryJobEventsSource()
    source.create("job-1", installation_id=ORG.installation_id)
    snapshot = await source.snapshot("job-1")
    assert snapshot is not None

    async def worker() -> None:
        await asyncio.sleep(0.12)
        await source.record(event(1, step=JobStep.POST_FEEDBACK, step_status=StepStatus.RUNNING))
        await source.record(event(2, status=ReviewJobStatus.COMPLETED))

    task = asyncio.create_task(worker())
    chunks = [
        chunk
        async for chunk in api.jobs._events(_ConnectedRequest(source), snapshot)  # type: ignore[arg-type]
    ]
    await task

    assert chunks[0].startswith("retry: ")
    assert ": ping\n\n" in chunks
    assert any("event: step" in chunk and '"stage":"publishing"' in chunk for chunk in chunks)
    assert chunks[-1].startswith("id: 2\nevent: end")


async def test_events_must_arrive_in_order() -> None:
    source = InMemoryJobEventsSource()
    source.create("job-1", installation_id=ORG.installation_id)
    await source.record(event(2))

    with pytest.raises(ValueError):
        await source.record(event(1))


def test_every_step_maps_to_a_console_stage() -> None:
    assert {ui_stage(step) for step in JobStep} == set(UiStage)
    assert ui_stage(JobStep.FETCH_DIFF) is UiStage.COLLECTING_CONTEXT
    assert ui_stage(JobStep.POSTPROCESS) is UiStage.VALIDATING
