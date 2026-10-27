import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from adapters.llm.config import LLMSettings
from domain.errors import (
    AllProvidersFailedError,
    LLMInputTooLargeError,
    LLMOutputError,
    ProviderUnavailableError,
)
from domain.repository_rules import RULES_MAX_BYTES
from evals.loader import load_case
from evals.models import Case
from evals.runner import LiveRunner, classify_failure
from evals.scoring import RunStatus, Verdict
from tests.conftest import FAKE_KEY_A, VALID_FINDING, chat_response, replies, review_json

DATASET = Path(__file__).resolve().parents[2] / "test-prs-dataset"
ENV = {
    "LLM_PRIMARY_API_KEYS": FAKE_KEY_A,
    "LLM_PRIMARY_MODEL": "m",
    "LLM_PRIMARY_BASE_URL": "https://llm.example.test/api/v1",
}


@pytest.fixture
def sql_case() -> Case:
    return load_case(DATASET / "sec-sql-sort-injection")


def runner_for(*responses: httpx.Response | Exception) -> LiveRunner:
    transport = httpx.MockTransport(replies(*responses))
    return LiveRunner(LLMSettings.from_env(ENV), transport=transport)


async def test_a_matching_reply_is_a_true_positive(sql_case: Case) -> None:
    record = await runner_for(chat_response(review_json(VALID_FINDING))).run(sql_case)

    assert record.score.status is RunStatus.OK
    assert (record.score.tp, record.score.fp, record.score.fn) == (1, 0, 0)
    assert record.score.findings[0].verdict is Verdict.TP
    assert record.provider == "eurouter"
    assert record.error is None
    assert (record.with_suggestion, record.llm_calls) == (1, 1)
    assert record.input_tokens > 0


async def test_findings_the_parser_drops_are_counted(sql_case: Case) -> None:
    broken = {**VALID_FINDING, "severity": "catastrophic"}

    record = await runner_for(chat_response(review_json(VALID_FINDING, broken))).run(sql_case)

    assert record.score.dropped == 1
    assert record.score.tp == 1


async def test_a_reply_that_never_parses_is_schema_invalid(sql_case: Case) -> None:
    record = await runner_for(chat_response("not json"), chat_response("still not")).run(sql_case)

    assert record.score.status is RunStatus.SCHEMA_INVALID
    assert record.score.fn == 1
    assert record.error == "AllProvidersFailedError"


async def test_a_provider_outage_is_errored_not_schema_invalid(sql_case: Case) -> None:
    record = await runner_for(httpx.Response(503)).run(sql_case)

    assert record.score.status is RunStatus.ERRORED
    assert record.score.fn == 1


async def test_the_runner_reuses_one_gateway_across_cases_without_leaking_replies(
    sql_case: Case,
) -> None:
    broken = {**VALID_FINDING, "severity": "catastrophic"}
    runner = runner_for(
        chat_response(review_json(VALID_FINDING, broken)),
        chat_response(review_json(VALID_FINDING)),
    )

    first = await runner.run(sql_case)
    second = await runner.run(sql_case)

    assert (first.score.dropped, second.score.dropped) == (1, 0)


def test_failure_classification() -> None:
    output = LLMOutputError("eurouter", "bad")
    outage = ProviderUnavailableError("eurouter", "down")

    assert classify_failure(output) is RunStatus.SCHEMA_INVALID
    assert classify_failure(AllProvidersFailedError([output, output])) is RunStatus.SCHEMA_INVALID
    assert classify_failure(AllProvidersFailedError([output, outage])) is RunStatus.ERRORED
    assert classify_failure(outage) is RunStatus.ERRORED
    assert classify_failure(LLMInputTooLargeError(10, 5)) is RunStatus.ERRORED


async def test_no_message_text_reaches_the_results(sql_case: Case) -> None:
    from evals.report import render_summary, results_json

    record = await runner_for(chat_response(review_json(VALID_FINDING))).run(sql_case)
    rendered = render_summary([record]) + json.dumps(results_json([record]))

    assert VALID_FINDING["message"] not in rendered
    assert "whitelist the column names" not in rendered
    assert FAKE_KEY_A not in rendered


async def test_context_sources_are_sent_on_the_review_request(
    sql_case: Case, tmp_path: Path
) -> None:
    from evals.context import CaseContextSource

    checkout = tmp_path / "head"
    (checkout / "app").mkdir(parents=True)
    source = "def list_users(sort):\n    return sorted(sort)\n"
    (checkout / "app/users.py").write_text(source, encoding="utf-8")
    rules = tmp_path / "rules.md"
    rules.write_text("- Prefer safe sorting", encoding="utf-8")
    seen: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return chat_response(review_json(VALID_FINDING))

    await LiveRunner(
        LLMSettings.from_env(ENV),
        transport=httpx.MockTransport(transport),
        context_sources={sql_case.id: CaseContextSource(checkout=checkout, rules=rules)},
    ).run(sql_case)

    body = json.loads(seen[0].content)
    user = body["messages"][1]["content"]
    assert "safe sorting" in user
    assert "def list_users" in user


@pytest.mark.parametrize(
    "rules_content",
    [b"OVERSIZED_RULE_SECRET" + b"x" * RULES_MAX_BYTES, b"INVALID_UTF8_RULE_SECRET\xff"],
    ids=["oversized", "invalid-utf8"],
)
async def test_invalid_rules_files_are_ignored(
    sql_case: Case, tmp_path: Path, rules_content: bytes
) -> None:
    from evals.context import CaseContextSource

    rules = tmp_path / "rules.md"
    rules.write_bytes(rules_content)
    seen: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return chat_response(review_json(VALID_FINDING))

    record = await LiveRunner(
        LLMSettings.from_env(ENV),
        transport=httpx.MockTransport(transport),
        context_sources={sql_case.id: CaseContextSource(rules=rules)},
    ).run(sql_case)

    assert record.score.status is RunStatus.OK
    prompt = json.loads(seen[0].content)["messages"][1]["content"]
    assert "OVERSIZED_RULE_SECRET" not in prompt
    assert "INVALID_UTF8_RULE_SECRET" not in prompt


@pytest.mark.parametrize("escape", ["traversal", "absolute", "symlink"])
async def test_context_does_not_read_paths_outside_checkout(
    sql_case: Case, tmp_path: Path, escape: str
) -> None:
    from evals.context import CaseContextSource

    checkout = tmp_path / "checkout"
    app = checkout / "app"
    app.mkdir(parents=True)
    diff_path = "app/users.py"
    secret = tmp_path / "secret.py"
    secret.write_text("SECRET_OUTSIDE_CHECKOUT", encoding="utf-8")
    if escape == "traversal":
        diff_path = "../secret.py"
    elif escape == "absolute":
        diff_path = str(secret)
    else:
        (app / "users.py").symlink_to(secret)
    diff = sql_case.read_diff().replace("app/users.py", diff_path)
    fixture = tmp_path / "case.diff"
    fixture.write_text(diff, encoding="utf-8")
    case = replace(sql_case, fixture=fixture)
    seen: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return chat_response(review_json(VALID_FINDING))

    await LiveRunner(
        LLMSettings.from_env(ENV),
        transport=httpx.MockTransport(transport),
        context_sources={case.id: CaseContextSource(checkout=checkout)},
    ).run(case)

    assert "SECRET_OUTSIDE_CHECKOUT" not in json.loads(seen[0].content)["messages"][1]["content"]
