"""GET /jobs and GET /jobs/{job_id} over TestClient, with the in-memory job store."""

from typing import Any

from fastapi.testclient import TestClient

from adapters.jobs.memory import MemoryJobStore
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
from tests.worker.test_process import make_payload

ORG = make_organization("acme", "100", installation_id=42)


def client_for(clock: FixedClock, store: MemoryJobStore) -> TestClient:
    identity = FakeIdentityProvider(Profile(user=make_user(), organizations=(ORG,)))
    client = TestClient(build_test_app(clock, identity, jobs=store), base_url=TEST_ORIGIN)
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
    assert client.get("/jobs/nope").status_code == 404
    assert client.get("/jobs", params={"status": "DONE"}).status_code == 400
    assert client.get("/jobs", params={"cursor": "%%%"}).status_code == 400
    assert client.get("/jobs", params={"limit": 0}).status_code == 400
    assert client.get("/jobs", params={"from": "yesterday"}).status_code == 400
