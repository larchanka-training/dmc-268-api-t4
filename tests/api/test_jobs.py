"""GET /jobs and GET /jobs/{job_id} over TestClient, with the in-memory job store."""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

import api.jobs
from adapters.jobs.memory import MemoryJobStore
from adapters.memory.review_comments import MemoryReviewCommentStore
from domain.jobs import JobStep, StepRecord, StepStatus
from domain.models import FindingSuggestion, Severity
from domain.ports import JobStats, Profile
from tests.api.test_repositories import sign_in
from tests.conftest import (
    TEST_ORIGIN,
    FakeIdentityProvider,
    FixedClock,
    build_test_app,
    make_organization,
    make_user,
)
from tests.ports.test_review_comment_store import make_new
from tests.worker.test_process import make_payload

ORG = make_organization("acme", "100", installation_id=42)


def client_for(
    clock: FixedClock, store: MemoryJobStore, findings: MemoryReviewCommentStore | None = None
) -> TestClient:
    identity = FakeIdentityProvider(Profile(user=make_user(), organizations=(ORG,)))
    app = build_test_app(clock, identity, jobs=store, findings=findings)
    client = TestClient(app, base_url=TEST_ORIGIN)
    sign_in(client)
    return client


async def seed(clock: FixedClock, store: MemoryJobStore, **overrides: Any) -> str:
    job_id = await store.enqueue(make_payload(**overrides))
    assert job_id is not None
    clock.advance(60)
    return job_id


async def test_the_list_is_newest_first_and_pages_with_a_cursor(auth_clock: FixedClock) -> None:
    store = MemoryJobStore(clock=auth_clock)
    first = await seed(auth_clock, store, delivery_id="d-1", pr_number=1)
    second = await seed(auth_clock, store, delivery_id="d-2", pr_number=2)
    third = await seed(auth_clock, store, delivery_id="d-3", pr_number=3)
    client = client_for(auth_clock, store)

    page = client.get("/jobs", params={"limit": 2}).json()
    rest = client.get("/jobs", params={"limit": 2, "cursor": page["next_cursor"]}).json()

    assert [item["id"] for item in page["items"]] == [third, second]
    assert [item["id"] for item in rest["items"]] == [first]
    assert rest["next_cursor"] is None


async def test_a_job_carries_its_pull_request_and_status(auth_clock: FixedClock) -> None:
    store = MemoryJobStore(clock=auth_clock)
    job_id = await seed(auth_clock, store, author_login="octocat", head_ref="feature")
    client = client_for(auth_clock, store)

    [item] = client.get("/jobs").json()["items"]

    assert item["id"] == job_id
    assert item["status"] == "QUEUED"
    assert item["trigger"] == "webhook"
    assert item["finding_count"] == 0
    assert item["pull_request"]["repo_full_name"] == "octocat/hello-world"
    assert item["pull_request"]["number"] == 7
    assert item["pull_request"]["author"] == "octocat"
    assert item["pull_request"]["url"] == "https://github.com/octocat/hello-world/pull/7"


async def test_filters_narrow_the_list(auth_clock: FixedClock) -> None:
    store = MemoryJobStore(clock=auth_clock)
    await seed(auth_clock, store, delivery_id="d-1", pr_number=1, repo_id=1)
    active = await seed(
        auth_clock, store, delivery_id="d-2", pr_number=2, repo_id=2, repo_full_name="acme/api"
    )
    done = await seed(auth_clock, store, delivery_id="d-3", pr_number=3, repo_id=3)
    claimed = await store.claim("w-1")
    assert claimed is not None and claimed.job_id != done
    # Finish the oldest claimed job, so one is terminal and two are still in flight.
    await store.complete(claimed.job_id, JobStats(1, 1, 1, 1, ()), worker_id="w-1")
    client = client_for(auth_clock, store)

    def ids(**params: Any) -> list[str]:
        return [item["id"] for item in client.get("/jobs", params=params).json()["items"]]

    assert claimed.job_id not in ids(status="active")
    assert ids(status="COMPLETED") == [claimed.job_id]
    assert ids(repository="acme/api") == [active]
    assert ids(q="#3") == [done]


async def test_other_installations_jobs_are_hidden(auth_clock: FixedClock) -> None:
    store = MemoryJobStore(clock=auth_clock)
    hidden = await seed(auth_clock, store, installation_id=999)
    client = client_for(auth_clock, store)

    assert client.get("/jobs").json()["items"] == []
    assert client.get(f"/jobs/{hidden}").status_code == 404


async def test_one_job_has_its_steps_and_bad_input_is_rejected(auth_clock: FixedClock) -> None:
    store = MemoryJobStore(clock=auth_clock)
    job_id = await seed(auth_clock, store)
    client = client_for(auth_clock, store)

    detail = client.get(f"/jobs/{job_id}").json()

    assert detail["id"] == job_id
    assert detail["steps"] == [] and detail["findings"] == []
    assert detail["stage"] is None
    assert client.get("/jobs/nope").status_code == 404
    assert client.get("/jobs", params={"status": "DONE"}).status_code == 400
    assert client.get("/jobs", params={"cursor": "%%%"}).status_code == 400
    assert client.get("/jobs", params={"limit": 0}).status_code == 400
    assert client.get("/jobs", params={"from": "yesterday"}).status_code == 400


def test_a_step_and_the_job_s_current_stage_use_the_console_vocabulary() -> None:
    # The in-memory store records no steps (see MemoryJobStore.steps), so the mapping
    # from a step to its console stage is exercised directly instead of over TestClient.
    at = datetime(2026, 1, 1, tzinfo=UTC)
    first = StepRecord(step=JobStep.BUILD_CONTEXT, attempt=1, status=StepStatus.OK, started_at=at)
    latest = StepRecord(step=JobStep.LLM_CALL, attempt=1, status=StepStatus.RUNNING, started_at=at)

    assert api.jobs._step_json(latest)["stage"] == "reviewing"
    # jobs.steps() orders by (attempt, seq): the last element is the most recent step.
    assert api.jobs._current_stage((first, latest)) == "reviewing"
    assert api.jobs._current_stage(()) is None


async def test_one_job_returns_its_published_findings_most_severe_first(
    auth_clock: FixedClock,
) -> None:
    store = MemoryJobStore(clock=auth_clock)
    findings = MemoryReviewCommentStore(store, clock=auth_clock)
    job_id = await seed(auth_clock, store)
    await findings.record(
        job_id,
        [
            make_new("a" * 64, path="b.py", severity=Severity.LOW),
            make_new(
                "b" * 64,
                path="a.py",
                line=8,
                severity=Severity.CRITICAL,
                suggestion=FindingSuggestion(before=("x = 1",), after=("x = 2",)),
                start_line=7,
                comment_id=None,
                review_id=900,
            ),
            make_new("c" * 64, path="c.py", comment_id=None, review_id=None),
        ],
    )
    client = client_for(auth_clock, store, findings)

    detail = client.get(f"/jobs/{job_id}").json()

    pull_request = "https://github.com/octocat/hello-world/pull/7"
    first, second, third = detail["findings"]
    assert set(first) == {
        "id",
        "path",
        "line",
        "start_line",
        "side",
        "severity",
        "category",
        "message",
        "suggestion",
        "published_at",
        "external_url",
    }
    uuid.UUID(first["id"])
    assert {key: first[key] for key in ("path", "line", "start_line", "side")} == {
        "path": "a.py",
        "line": 8,
        "start_line": 7,
        "side": "new",
    }
    assert (first["severity"], first["category"]) == ("critical", "correctness")
    assert first["message"] == f"Rendered comment <!-- {'b' * 64} -->"
    assert first["suggestion"] == {"before": ["x = 1"], "after": ["x = 2"]}
    assert first["published_at"] == auth_clock.now().isoformat()
    assert first["external_url"] == f"{pull_request}#pullrequestreview-900"
    assert second["path"] == "c.py" and second["external_url"] == pull_request
    assert third["path"] == "b.py" and third["suggestion"] is None
    assert third["external_url"] == f"{pull_request}#discussion_r5001"
