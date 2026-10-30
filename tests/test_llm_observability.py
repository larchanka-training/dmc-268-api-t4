"""The LLM gateway's metrics and spans (docs/observability.md § Metrics, § Review
pipeline): one counted request and one ``llm.attempt`` span per HTTP request, tokens as
the provider reported them, key rotations, a purpose per call, and the rule screen and
verifier as spans of their own. Counts and names only: no prompt or reply text."""

import json
from typing import Any

import httpx
import pytest
import sentry_sdk

from adapters.llm.config import LLMSettings
from adapters.llm.factory import build_gateway
from adapters.llm.gateway import FallbackLLMGateway, review_with_provider
from adapters.llm.jev import TYPESAFE, JevClient
from adapters.llm.prompt import ChatMessage
from adapters.llm.reflection import VerificationReport
from adapters.llm.rule_screen import RuleScreenReport
from adapters.llm.rules import Rule
from adapters.observability import metrics
from domain.errors import ProviderUnavailableError
from domain.models import VerificationOutcome
from domain.ports import ReviewRequest
from tests.conftest import (
    FAKE_KEY_A,
    FAKE_KEY_B,
    CaptureTransport,
    FakeClock,
    RecordingTransport,
    chat_response,
    make_provider,
    replies,
)

MESSAGES: list[ChatMessage] = [{"role": "user", "content": "PROMPT-MARKER review this"}]
PROVIDER, MODEL = "eurouter", "eurouter-model"
REVIEW_JSON = json.dumps({"summary": "REPLY-MARKER fine", "findings": []})


def requests(
    outcome: str, purpose: str = "review", provider: str = PROVIDER, model: str = MODEL
) -> float:
    value = metrics.WORKER_REGISTRY.get_sample_value(
        "llm_requests_total",
        {"provider": provider, "model": model, "purpose": purpose, "outcome": outcome},
    )
    return value or 0.0


def tokens(
    direction: str, purpose: str = "review", provider: str = PROVIDER, model: str = MODEL
) -> float:
    value = metrics.WORKER_REGISTRY.get_sample_value(
        "llm_tokens_total",
        {"provider": provider, "model": model, "purpose": purpose, "direction": direction},
    )
    return value or 0.0


def rotations(provider: str = PROVIDER) -> float:
    value = metrics.WORKER_REGISTRY.get_sample_value(
        "llm_key_rotations_total", {"provider": provider}
    )
    return value or 0.0


def with_usage(content: str, prompt_tokens: int, completion_tokens: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    )


async def test_a_reply_counts_one_ok_request_and_its_tokens() -> None:
    before = (requests("ok"), tokens("input"), tokens("output"))
    provider = make_provider(RecordingTransport(replies(with_usage("hi", 120, 30))))

    await provider.complete(MESSAGES)

    assert (requests("ok"), tokens("input"), tokens("output")) == (
        before[0] + 1,
        before[1] + 120,
        before[2] + 30,
    )


async def test_each_set_aside_key_is_a_request_and_a_rotation(clock: FakeClock) -> None:
    before = (requests("rate_limited"), requests("key_rejected"), requests("ok"), rotations())
    transport = RecordingTransport(
        replies(
            httpx.Response(429, headers={"Retry-After": "30"}),
            httpx.Response(401),
            chat_response("ok"),
        )
    )
    provider = make_provider(
        transport, keys=(FAKE_KEY_A, FAKE_KEY_B, "sk-" + "c" * 30), clock=clock
    )

    await provider.complete(MESSAGES)

    assert requests("rate_limited") == before[0] + 1
    assert requests("key_rejected") == before[1] + 1
    assert requests("ok") == before[2] + 1
    assert rotations() == before[3] + 2


@pytest.mark.parametrize(
    "failure",
    [httpx.Response(500), httpx.Response(400, text="bad"), httpx.ConnectError("refused")],
)
async def test_a_failed_request_counts_as_error(failure: httpx.Response | Exception) -> None:
    before = requests("error")

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(failure, Exception):
            raise failure
        return failure

    with pytest.raises(ProviderUnavailableError):
        await make_provider(httpx.MockTransport(handler)).complete(MESSAGES)

    assert requests("error") == before + 1


async def test_the_corrective_retry_is_counted_as_correction() -> None:
    before = (requests("ok", "review"), requests("ok", "correction"))
    provider = make_provider(
        RecordingTransport(replies(chat_response("not json"), chat_response(REVIEW_JSON)))
    )
    request = ReviewRequest(diff_text="", pr_title="t", pr_description="d")

    await review_with_provider(provider, request)

    assert requests("ok", "review") == before[0] + 1
    assert requests("ok", "correction") == before[1] + 1


async def test_each_request_is_an_llm_attempt_span_with_counts_never_text(
    sentry_capture: CaptureTransport,
) -> None:
    provider = make_provider(RecordingTransport(replies(with_usage(REVIEW_JSON, 120, 30))))

    with sentry_sdk.start_transaction(name="review.job", op="queue.task"):
        await provider.complete(MESSAGES, purpose="summary")
    sentry_sdk.flush()

    [transaction] = sentry_capture.payloads("transaction")
    [span] = [s for s in transaction["spans"] if s["op"] == "llm.attempt"]
    assert span["description"] == f"{PROVIDER} {MODEL}"
    data = span["data"]
    assert (data["provider"], data["model"], data["purpose"], data["outcome"]) == (
        PROVIDER,
        MODEL,
        "summary",
        "ok",
    )
    assert (data["input_tokens"], data["output_tokens"]) == (120, 30)
    dumped = sentry_capture.dump()
    assert "PROMPT-MARKER" not in dumped
    assert "REPLY-MARKER" not in dumped


async def test_typesafe_requests_are_counted_by_purpose() -> None:
    before = (
        requests("ok", "verify", TYPESAFE, "jev-model"),
        requests("error", "rule_screen", TYPESAFE, "jev-model"),
        tokens("input", "verify", TYPESAFE, "jev-model"),
    )
    answers = iter(
        [
            httpx.Response(
                200, json={"model": "jev-model", "answers": {}, "usage": {"input_tokens": 50}}
            ),
            httpx.Response(503),
        ]
    )
    client = JevClient(
        api_key="ts-key", model="jev-model", transport=httpx.MockTransport(lambda _: next(answers))
    )

    await client.ask_noul("state", {}, purpose="verify")
    with pytest.raises(httpx.HTTPError):
        await client.ask_noul("state", {}, purpose="rule_screen")

    assert requests("ok", "verify", TYPESAFE, "jev-model") == before[0] + 1
    assert requests("error", "rule_screen", TYPESAFE, "jev-model") == before[1] + 1
    assert tokens("input", "verify", TYPESAFE, "jev-model") == before[2] + 50


def series(name: str, **labels: str) -> float | None:
    return metrics.WORKER_REGISTRY.get_sample_value(name, labels)


def test_building_the_gateway_starts_its_series_at_zero() -> None:
    env: dict[str, Any] = {
        "LLM_PRIMARY_API_KEYS": "sk-" + "z" * 30,
        "LLM_PRIMARY_MODEL": "start-at-zero-model",
    }
    settings = LLMSettings.from_env(env)

    build_gateway(settings)

    name = settings.primary.name
    for purpose in ("review", "correction", "summary"):
        assert (
            series(
                "llm_requests_total",
                provider=name,
                model="start-at-zero-model",
                purpose=purpose,
                outcome="error",
            )
            == 0.0
        )
    # Shared with the other tests' provider, so only its existence is certain here.
    assert series("llm_key_rotations_total", provider=name) is not None


class PassingScreen:
    async def screen(self, rules: Any) -> RuleScreenReport:
        return RuleScreenReport(VerificationOutcome.PASSED, tuple(rules)[1:], dropped=1)


class RejectingVerifier:
    async def verify(self, request: Any, rules: Any, findings: Any) -> VerificationReport:
        return VerificationReport(VerificationOutcome.PASSED, (), rejected=2)


async def test_the_rule_screen_and_the_verifier_are_spans_of_their_own(
    sentry_capture: CaptureTransport,
) -> None:
    gateway = FallbackLLMGateway(
        [make_provider(RecordingTransport(replies(chat_response(REVIEW_JSON))))],
        verifier=RejectingVerifier(),
        rule_screen=PassingScreen(),
    )
    rules = [Rule(id="R1", section="Security", text="RULE-MARKER"), Rule("R2", "Style", "x")]

    with sentry_sdk.start_transaction(name="review.job", op="queue.task"):
        await gateway._screen_rules(rules)
        await gateway.review(ReviewRequest(diff_text="", pr_title="t", pr_description="d"))
    sentry_sdk.flush()

    [transaction] = sentry_capture.payloads("transaction")
    spans = {span["op"]: span["data"] for span in transaction["spans"]}
    screen = spans["llm.rule_screen"]
    assert {k: screen[k] for k in ("rules_in", "rules_dropped", "outcome")} == {
        "rules_in": 2,
        "rules_dropped": 1,
        "outcome": "passed",
    }
    assert {k: spans["llm.verify"][k] for k in ("findings_in", "findings_rejected", "outcome")} == {
        "findings_in": 0,
        "findings_rejected": 2,
        "outcome": "passed",
    }
    assert "RULE-MARKER" not in sentry_capture.dump()
