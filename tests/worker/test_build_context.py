"""The BUILD_CONTEXT step of a claimed job (review-context spec).

Everything here is fake: the git provider serves a diff, the contents port
serves rules and source from a dict, the symbol index is scripted. No network.
"""

from adapters.jobs.memory import MemoryJobStore
from adapters.memory.job_events import InMemoryStepRecorder
from domain.diff import parse_diff
from domain.diff_filter import filter_files
from domain.errors import ForgePermissionError, ForgeUnavailableError
from domain.jobs import JobStep
from domain.ports import ContextBlockKind
from domain.repository_rules import RULES_MAX_BYTES, RULES_PATH
from tests.conftest import FakeRepositoryContents, FixedClock
from tests.worker.test_process import BASE_SHA, HEAD_SHA, make_payload
from tests.worker.test_review_stage import FakeGateway, FakePublisher
from worker.process import ContextStage, ReviewStage, process_claimed_job

REPO = "octocat/hello-world"

RULES_MD = b"# Security\n- Bind every SQL parameter.\n"

PY_SOURCE = "\n".join(
    [
        "def helper(x):",  # 1
        "    '''Docs.'''",  # 2
        "    return x + 1",  # 3
        "",  # 4
        "def run(a):",  # 5
        "    return helper(a)",  # 6
    ]
)

# A diff that adds one line inside run() of app/x.py.
GOOD_DIFF = (
    "diff --git a/app/x.py b/app/x.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/app/x.py\n"
    "+++ b/app/x.py\n"
    "@@ -5,2 +5,3 @@ def run(a):\n"
    "     return helper(a)\n"
    "+    return helper(a + 1)\n"
    " \n"
)


class FakeGitProvider:
    """Serves one pull request whose diff exercises app/x.py."""

    def __init__(self, diff_text: str) -> None:
        self._diff = diff_text

    async def fetch_pull_request(self, installation_id: int, repo_full_name: str, number: int):
        from domain.ports import PullRequestContext

        return PullRequestContext(
            installation_id=installation_id,
            repo_full_name=repo_full_name,
            pr_number=number,
            title="Add the call",
            description="",
            head_sha=HEAD_SHA,
            base_sha=BASE_SHA,
            head_ref="feature/x",
            base_ref="main",
            author_login=None,
            author_external_id=None,
            author_association=None,
            commits=(),
            diff_text=self._diff,
        )


class RealIndex:
    """SymbolIndex over the real tree-sitter implementation, scoped to one file."""

    def __init__(self) -> None:
        from adapters.context import TreeSitterSymbolIndex

        self._inner = TreeSitterSymbolIndex()

    def supports(self, path: str) -> bool:
        return self._inner.supports(path)

    def enclosing(self, path: str, source: str, lines):
        return self._inner.enclosing(path, source, lines)

    def called_from(self, path: str, source: str, lines):
        return self._inner.called_from(path, source, lines)


def stage_for(contents, budget: int = 10_000) -> ContextStage:
    return ContextStage(contents=contents, symbols=RealIndex(), context_budget_tokens=budget)


async def run_job(
    clock: FixedClock,
    contents: FakeRepositoryContents,
    *,
    budget: int = 10_000,
    steps: InMemoryStepRecorder | None = None,
):
    store = MemoryJobStore(clock=clock)
    job_id = await store.enqueue(make_payload())
    claimed = await store.claim("w-1")
    gateway = FakeGateway()
    publisher = FakePublisher()
    completed = await process_claimed_job(
        claimed,
        FakeGitProvider(GOOD_DIFF),
        store,
        review=ReviewStage(gateway=gateway, publisher=publisher),
        context_stage=stage_for(contents, budget),
        steps=steps,
    )
    job = await store.get(job_id)
    return completed, job, gateway, publisher


async def test_rules_and_ast_context_reach_the_gateway(auth_clock: FixedClock) -> None:
    contents = FakeRepositoryContents(
        {
            (REPO, RULES_PATH, BASE_SHA): RULES_MD,
            (REPO, "app/x.py", HEAD_SHA): PY_SOURCE.encode(),
        }
    )
    steps = InMemoryStepRecorder()

    completed, job, gateway, publisher = await run_job(auth_clock, contents, steps=steps)

    assert completed is True
    assert job is not None and job.status.value == "COMPLETED"
    # The Check Run summary carries the one rules line ([R11]).
    (target, plan) = publisher.calls[0]
    assert plan.rules_audit is not None
    assert plan.rules_audit.ref == BASE_SHA
    assert f"Rules: {RULES_PATH} @ {BASE_SHA} — 1 rules." in plan.summary
    (request,) = gateway.requests
    assert request.rules_markdown is not None
    assert "Bind every SQL parameter" in request.rules_markdown
    assert len(request.context) == 1
    file_context = request.context[0]
    assert file_context.path == "app/x.py"
    kinds = {block.kind for block in file_context.blocks}
    assert ContextBlockKind.ENCLOSING in kinds
    assert ContextBlockKind.CALLED in kinds
    # The step row carries the counters, including the rules fetch.
    build_records = [r for r in steps.records(job.job_id) if r["step"] is JobStep.BUILD_CONTEXT]
    assert len(build_records) == 1
    metrics = build_records[0]["metrics"]
    assert metrics is not None
    assert metrics["context_files"] == 1
    assert metrics["context_fetches"] == 1
    assert metrics["tier1_symbols"] >= 1
    assert metrics["context_cut"] == "none"


async def test_absent_rules_and_source_still_complete_the_review(auth_clock: FixedClock) -> None:
    contents = FakeRepositoryContents({})

    completed, job, gateway, publisher = await run_job(auth_clock, contents)

    assert completed is True
    (request,) = gateway.requests
    assert request.rules_markdown is None
    assert request.context == ()
    # An absent rules file means no rules line in the Check Run ([R11]).
    (target, plan) = publisher.calls[0]
    assert plan.rules_audit is None
    assert "Rules:" not in plan.summary


async def test_a_permission_refusal_means_no_context_not_a_failure(auth_clock: FixedClock) -> None:
    contents = FakeRepositoryContents(error=ForgePermissionError("github", "HTTP 403"))

    completed, job, gateway, _publisher = await run_job(auth_clock, contents)

    assert completed is True
    (request,) = gateway.requests
    assert request.rules_markdown is None
    assert request.context == ()


async def test_a_transient_forge_error_sends_the_job_to_retrying(auth_clock: FixedClock) -> None:
    contents = FakeRepositoryContents(error=ForgeUnavailableError("github", "HTTP 503"))

    completed, job, _, _ = await run_job(auth_clock, contents)

    assert completed is False
    assert job is not None and job.status.value == "RETRYING"
    assert job.error_kind == "forge_unavailable"


async def test_rejected_rules_do_not_block_the_review(auth_clock: FixedClock) -> None:
    contents = FakeRepositoryContents({(REPO, RULES_PATH, BASE_SHA): b"\xff\xfe not utf8"})

    completed, job, gateway, publisher = await run_job(auth_clock, contents)

    assert completed is True
    (request,) = gateway.requests
    assert request.rules_markdown is None
    # A rejected file still gets its "ignored" line, saying why ([R11]).
    (target, plan) = publisher.calls[0]
    assert plan.rules_audit is not None
    assert f"Rules: {RULES_PATH} @ {BASE_SHA} ignored — not valid UTF-8." in plan.summary


async def test_rules_are_fetched_at_base_sha_and_sources_at_head(auth_clock: FixedClock) -> None:
    contents = FakeRepositoryContents(
        {
            (REPO, RULES_PATH, BASE_SHA): RULES_MD,
            (REPO, "app/x.py", HEAD_SHA): PY_SOURCE.encode(),
        }
    )

    await run_job(auth_clock, contents)

    refs = [(call[2], call[3]) for call in contents.calls]
    assert (RULES_PATH, BASE_SHA) in refs
    assert ("app/x.py", HEAD_SHA) in refs
    assert all(call[4] == RULES_MAX_BYTES for call in contents.calls if call[2] == RULES_PATH)


def test_the_fixture_diff_parses_and_keeps_the_file() -> None:
    files = parse_diff(GOOD_DIFF)
    kept = filter_files(files)
    assert [diff.new_path for diff in kept.kept] == ["app/x.py"]
