import json
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


# --- The suggestion pass and the message length (issue #88) ----------------------------------

QUERY = '    rows = conn.execute(f"SELECT id, name, email FROM users ORDER BY {sort}").fetchall()'
BARE_FINDING = {**VALID_FINDING, "suggestion": None}


def suggestion_reply(before: str) -> httpx.Response:
    entry = {"id": 1, "suggestion": {"before": [before], "after": ["    rows = []"]}}
    return chat_response(json.dumps({"suggestions": [entry]}))


async def test_the_suggestion_pass_runs_on_findings_without_a_fix(sql_case: Case) -> None:
    runner = runner_for(chat_response(review_json(BARE_FINDING)), suggestion_reply(QUERY))

    record = await runner.run(sql_case)

    assert (record.with_suggestion, record.applicable_suggestions) == (1, 1)
    assert record.llm_calls == 2
    assert (record.score.tp, record.score.dropped) == (1, 0)
    assert record.message_lengths == (len(VALID_FINDING["message"]),)


async def test_a_first_pass_suggestion_that_misses_the_diff_is_not_applicable(
    sql_case: Case,
) -> None:
    record = await runner_for(chat_response(review_json(VALID_FINDING))).run(sql_case)

    assert (record.with_suggestion, record.applicable_suggestions) == (1, 0)


async def test_a_failed_suggestion_pass_still_scores_the_review(sql_case: Case) -> None:
    runner = runner_for(chat_response(review_json(BARE_FINDING)), httpx.Response(503))

    record = await runner.run(sql_case)

    assert record.score.status is RunStatus.OK
    assert (record.score.tp, record.with_suggestion) == (1, 0)


async def test_the_suggestion_pass_can_be_switched_off(sql_case: Case) -> None:
    settings = LLMSettings.from_env({**ENV, "LLM_SUGGESTION_PASS": "false"})
    transport = httpx.MockTransport(replies(chat_response(review_json(BARE_FINDING))))

    record = await LiveRunner(settings, transport=transport).run(sql_case)

    assert (record.llm_calls, record.with_suggestion) == (1, 0)


async def test_the_summary_reports_suggestions_and_the_median_message_length(
    sql_case: Case,
) -> None:
    from evals.report import render_summary, results_json

    runner = runner_for(chat_response(review_json(BARE_FINDING)), suggestion_reply(QUERY))
    record = await runner.run(sql_case)

    summary = render_summary([record])
    corpus = results_json([record])["corpus"]

    assert "- findings with a suggestion GitHub can apply: 100.0% (1 of 1)" in summary
    assert f"- median message length: {len(VALID_FINDING['message'])} characters" in summary
    assert corpus["applicable_suggestions"] == 1
    assert corpus["median_message_length"] == len(VALID_FINDING["message"])
    assert "rows = []" not in summary + json.dumps(results_json([record]))
