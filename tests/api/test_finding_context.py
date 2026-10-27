"""GET /jobs/{job_id}/findings/{finding_id}/context and /jobs/{job_id}/file-lines, with fakes."""

from fastapi.testclient import TestClient

from adapters.jobs.memory import MemoryJobStore
from adapters.memory.review_comments import MemoryReviewCommentStore
from domain.errors import ForgeUnavailableError, InstallationGoneError
from domain.finding_context import FindingContextService
from domain.ports import Profile
from tests.api.test_repositories import sign_in
from tests.conftest import (
    TEST_ORIGIN,
    FakeIdentityProvider,
    FakeRepositoryContents,
    FixedClock,
    build_test_app,
    make_organization,
    make_user,
)
from tests.ports.test_review_comment_store import make_new
from tests.worker.test_process import BASE_SHA, HEAD_SHA, make_payload

ORG = make_organization("acme", "100", installation_id=42)
REPO = "octocat/hello-world"
PATH = "app/service.py"

# Lines 1..20 of the reviewed file; line 10 is new, the old line 10 was removed.
FILE = "".join(f"line {n}\n" for n in range(1, 21)).encode()
DIFF = (
    f"diff --git a/{PATH} b/{PATH}\n"
    "index 1111111..2222222 100644\n"
    f"--- a/{PATH}\n"
    f"+++ b/{PATH}\n"
    "@@ -5,11 +5,11 @@ def handler():\n"
    " line 5\n line 6\n line 7\n line 8\n line 9\n"
    "-old 10\n"
    "+line 10\n"
    " line 11\n line 12\n line 13\n line 14\n line 15\n"
)


class FakeCompareDiffs:
    def __init__(self, diff: str | None = DIFF, *, error: Exception | None = None) -> None:
        self.diff = diff
        self.error = error
        self.calls: list[tuple[int, str, str, str]] = []

    async def compare_diff(
        self, installation_id: int, repo_full_name: str, base_sha: str, head_sha: str
    ) -> str | None:
        self.calls.append((installation_id, repo_full_name, base_sha, head_sha))
        if self.error is not None:
            raise self.error
        return self.diff


async def client_for(
    clock: FixedClock,
    *,
    diffs: FakeCompareDiffs | None = None,
    files: dict[tuple[str, str, str], bytes] | None = None,
    contents_error: Exception | None = None,
    line: int = 10,
    start_line: int | None = None,
    wired: bool = True,
    **payload: object,
) -> tuple[TestClient, str, str, FakeCompareDiffs]:
    store = MemoryJobStore(clock=clock)
    job_id = await store.enqueue(make_payload(**payload))
    assert job_id is not None
    findings = MemoryReviewCommentStore(store, clock=clock)
    await findings.record(job_id, [make_new("a" * 64, path=PATH, line=line, start_line=start_line)])
    (finding,) = await findings.for_job(job_id)
    diffs = diffs or FakeCompareDiffs()
    contents = FakeRepositoryContents(
        {(REPO, PATH, HEAD_SHA): FILE} if files is None else files, error=contents_error
    )
    identity = FakeIdentityProvider(Profile(user=make_user(), organizations=(ORG,)))
    app = build_test_app(
        clock,
        identity,
        jobs=store,
        findings=findings,
        finding_context=(
            FindingContextService(store, findings, diffs, contents) if wired else None
        ),
    )
    client = TestClient(app, base_url=TEST_ORIGIN)
    sign_in(client)
    return client, job_id, finding.id, diffs


async def test_context_has_the_agreed_shape(auth_clock: FixedClock) -> None:
    client, job_id, finding_id, diffs = await client_for(auth_clock)

    response = client.get(f"/jobs/{job_id}/findings/{finding_id}/context")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert diffs.calls == [(42, REPO, BASE_SHA, HEAD_SHA)]
    assert {key: body[key] for key in ("path", "old_path", "status", "file_line_count")} == {
        "path": PATH,
        "old_path": None,
        "status": "modified",
        "file_line_count": 20,
    }
    (hunk,) = body["hunks"]
    # Trimmed to the default three unchanged lines on each side of the change.
    assert {key: hunk[key] for key in hunk if key != "lines"} == {
        "id": "hunk-7-7",
        "old_start": 7,
        "old_lines": 7,
        "new_start": 7,
        "new_lines": 7,
        "header": "def handler():",
    }
    assert hunk["lines"][:4] == [
        {"type": "context", "old_line": 7, "new_line": 7, "content": "line 7"},
        {"type": "context", "old_line": 8, "new_line": 8, "content": "line 8"},
        {"type": "context", "old_line": 9, "new_line": 9, "content": "line 9"},
        {"type": "removed", "old_line": 10, "new_line": None, "content": "old 10"},
    ]
    assert hunk["lines"][4] == {
        "type": "added",
        "old_line": None,
        "new_line": 10,
        "content": "line 10",
    }
    assert len(hunk["lines"]) == 8


async def test_context_lines_widen_the_hunk_up_to_the_diff(auth_clock: FixedClock) -> None:
    client, job_id, finding_id, _ = await client_for(auth_clock)

    body = client.get(f"/jobs/{job_id}/findings/{finding_id}/context?context_lines=10").json()

    (hunk,) = body["hunks"]
    assert (hunk["old_start"], hunk["old_lines"], hunk["new_start"], hunk["new_lines"]) == (
        5,
        11,
        5,
        11,
    )


async def test_context_lines_out_of_range_are_400(auth_clock: FixedClock) -> None:
    client, job_id, finding_id, _ = await client_for(auth_clock)

    response = client.get(f"/jobs/{job_id}/findings/{finding_id}/context?context_lines=51")

    assert response.status_code == 400
    assert response.json() == {"error": "bad_context_lines"}


async def test_a_gone_commit_is_409(auth_clock: FixedClock) -> None:
    client, job_id, finding_id, _ = await client_for(auth_clock, diffs=FakeCompareDiffs(None))

    response = client.get(f"/jobs/{job_id}/findings/{finding_id}/context")

    assert response.status_code == 409
    assert response.json() == {"error": "context_unavailable"}


async def test_a_gone_installation_is_409(auth_clock: FixedClock) -> None:
    gone = FakeCompareDiffs(error=InstallationGoneError(42, "HTTP 403"))
    client, job_id, finding_id, _ = await client_for(auth_clock, diffs=gone)

    assert client.get(f"/jobs/{job_id}/findings/{finding_id}/context").status_code == 409


async def test_a_line_no_longer_in_the_diff_is_409(auth_clock: FixedClock) -> None:
    client, job_id, finding_id, _ = await client_for(auth_clock, line=19)

    assert client.get(f"/jobs/{job_id}/findings/{finding_id}/context").status_code == 409


async def test_a_forge_failure_is_502(auth_clock: FixedClock) -> None:
    down = FakeCompareDiffs(error=ForgeUnavailableError("github", "HTTP 503"))
    client, job_id, finding_id, _ = await client_for(auth_clock, diffs=down)

    response = client.get(f"/jobs/{job_id}/findings/{finding_id}/context")

    assert response.status_code == 502
    assert response.json() == {"error": "forge_unavailable"}


async def test_an_unreadable_file_falls_back_to_the_diff_s_line_count(
    auth_clock: FixedClock,
) -> None:
    client, job_id, finding_id, _ = await client_for(auth_clock, files={})

    body = client.get(f"/jobs/{job_id}/findings/{finding_id}/context").json()

    assert body["file_line_count"] == 15


async def test_unknown_and_foreign_findings_are_404(auth_clock: FixedClock) -> None:
    client, job_id, _, diffs = await client_for(auth_clock)
    other, foreign_job, foreign_finding, foreign_diffs = await client_for(
        auth_clock, installation_id=999
    )

    unknown = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/jobs/{job_id}/findings/{unknown}/context").status_code == 404
    assert client.get(f"/jobs/{job_id}/findings/not-a-uuid/context").status_code == 404
    assert other.get(f"/jobs/{foreign_job}/findings/{foreign_finding}/context").status_code == (404)
    assert diffs.calls == [] and foreign_diffs.calls == []


async def test_without_the_forge_wired_context_is_502(auth_clock: FixedClock) -> None:
    client, job_id, finding_id, _ = await client_for(auth_clock, wired=False)

    assert client.get(f"/jobs/{job_id}/findings/{finding_id}/context").status_code == 502


async def test_file_lines_are_clamped_to_the_file(auth_clock: FixedClock) -> None:
    client, job_id, _, _ = await client_for(auth_clock)

    response = client.get(f"/jobs/{job_id}/file-lines", params={"path": PATH, "from": 18, "to": 40})

    assert response.status_code == 200
    assert response.json() == {"path": PATH, "from": 18, "lines": ["line 18", "line 19", "line 20"]}


async def test_file_lines_only_serve_a_file_with_a_finding(auth_clock: FixedClock) -> None:
    client, job_id, _, _ = await client_for(auth_clock)

    response = client.get(f"/jobs/{job_id}/file-lines", params={"path": ".env", "from": 1, "to": 5})

    assert response.status_code == 404


async def test_file_lines_reject_a_bad_range(auth_clock: FixedClock) -> None:
    client, job_id, _, _ = await client_for(auth_clock)

    def status(start: int, end: int) -> int:
        params = {"path": PATH, "from": start, "to": end}
        return client.get(f"/jobs/{job_id}/file-lines", params=params).status_code

    assert status(5, 4) == 400
    assert status(1, 1001) == 400
    assert status(0, 3) == 400


async def test_file_lines_of_a_gone_installation_are_409(auth_clock: FixedClock) -> None:
    client, job_id, _, _ = await client_for(
        auth_clock, contents_error=InstallationGoneError(42, "HTTP 401")
    )

    response = client.get(f"/jobs/{job_id}/file-lines", params={"path": PATH, "from": 1, "to": 3})

    assert response.status_code == 409
    assert response.json() == {"error": "context_unavailable"}


def test_context_needs_a_session(client: TestClient) -> None:
    assert client.get("/jobs/any/findings/any/context").status_code == 401
    assert client.get("/jobs/any/file-lines?path=a&from=1&to=2").status_code == 401
