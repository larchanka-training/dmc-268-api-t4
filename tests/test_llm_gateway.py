import json
import logging

import httpx
import pytest

from adapters.llm.config import LLMSettings, ProviderSettings
from adapters.llm.factory import build_gateway
from adapters.llm.gateway import FallbackLLMGateway
from adapters.llm.rule_screen import JevRuleScreen
from adapters.llm.rules import Rule
from domain.errors import (
    AllProvidersFailedError,
    LLMInputTooLargeError,
    LLMOutputError,
    ProviderUnavailableError,
)
from domain.ports import ContextBlock, ContextBlockKind, FileContext, ReviewRequest
from tests.conftest import (
    FAKE_KEY_A,
    FAKE_KEY_B,
    VALID_FINDING,
    RecordingTransport,
    chat_response,
    log_events,
    make_provider,
    replies,
    review_json,
)

REQUEST = ReviewRequest(diff_text="diff --git a/x b/x", pr_title="t", pr_description="d")


async def test_primary_503_falls_back_to_ollama(caplog: pytest.LogCaptureFixture) -> None:
    primary = make_provider(RecordingTransport(replies(httpx.Response(503))))
    fallback_transport = RecordingTransport(replies(chat_response(review_json(VALID_FINDING))))
    fallback = make_provider(fallback_transport, name="ollama", keys=())

    with caplog.at_level(logging.INFO, logger="adapters.llm"):
        result = await FallbackLLMGateway([primary, fallback]).review(REQUEST)

    assert result.provider == "ollama"
    assert result.model == "ollama-model"
    assert len(result.findings) == 1
    [fallback_record] = log_events(caplog, "llm.provider.fallback")
    assert fallback_record.next_provider == "ollama"
    [finished] = log_events(caplog, "llm.review.finished")
    assert (finished.provider, finished.model) == ("ollama", "ollama-model")


async def test_output_error_on_primary_also_falls_back() -> None:
    primary = make_provider(RecordingTransport(replies(chat_response("x"), chat_response("y"))))
    fallback = make_provider(
        RecordingTransport(replies(chat_response(review_json()))), name="ollama", keys=()
    )

    result = await FallbackLLMGateway([primary, fallback]).review(REQUEST)

    assert result.provider == "ollama"


async def test_all_providers_failed_keeps_every_cause(caplog: pytest.LogCaptureFixture) -> None:
    primary = make_provider(RecordingTransport(replies(httpx.ReadTimeout("slow"))))
    fallback = make_provider(
        RecordingTransport(replies(httpx.Response(500))), name="ollama", keys=()
    )

    with pytest.raises(AllProvidersFailedError) as excinfo:
        await FallbackLLMGateway([primary, fallback]).review(REQUEST)

    causes = excinfo.value.causes
    assert len(causes) == 2
    assert all(isinstance(cause, ProviderUnavailableError) for cause in causes)
    assert [cause.provider for cause in causes if isinstance(cause, ProviderUnavailableError)] == [
        "eurouter",
        "ollama",
    ]
    assert "timeout" in str(causes[0])
    assert "HTTP 500" in str(causes[1])
    # A warning: the job is retried, so this is not yet a Sentry event.
    [record] = log_events(caplog, "llm.call.failed")
    assert record.levelno == logging.WARNING
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


async def test_primary_success_does_not_touch_fallback() -> None:
    primary = make_provider(RecordingTransport(replies(chat_response(review_json()))))
    fallback_transport = RecordingTransport(replies())
    fallback = make_provider(fallback_transport, name="ollama", keys=())

    result = await FallbackLLMGateway([primary, fallback]).review(REQUEST)

    assert result.provider == "eurouter"
    assert fallback_transport.requests == []


async def test_single_provider_output_error_is_wrapped() -> None:
    primary = make_provider(RecordingTransport(replies(chat_response("x"), chat_response("y"))))

    with pytest.raises(AllProvidersFailedError) as excinfo:
        await FallbackLLMGateway([primary]).review(REQUEST)

    assert isinstance(excinfo.value.causes[0], LLMOutputError)


def test_gateway_requires_providers() -> None:
    with pytest.raises(ValueError):
        FallbackLLMGateway([])


async def test_factory_builds_keyed_primary_and_keyless_fallback() -> None:
    settings = LLMSettings(
        primary=ProviderSettings(
            name="eurouter",
            base_url="https://primary.test/api/v1",
            model="m1",
            api_keys=(FAKE_KEY_A, FAKE_KEY_B),
        ),
        fallback=ProviderSettings(name="ollama", base_url="http://fallback.test/v1", model="m2"),
        timeout_seconds=5,
        temperature=0.0,
        json_mode=True,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary.test":
            return httpx.Response(503)
        return chat_response(review_json())

    transport = RecordingTransport(handler)

    result = await build_gateway(settings, transport=transport).review(REQUEST)

    assert (result.provider, result.model) == ("ollama", "m2")
    primary_request, fallback_request = transport.requests
    assert primary_request.headers["Authorization"] == f"Bearer {FAKE_KEY_A}"
    assert "Authorization" not in fallback_request.headers


async def test_input_over_budget_is_refused_before_any_provider_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = RecordingTransport(replies(chat_response(review_json(VALID_FINDING))))
    big = ReviewRequest(diff_text="x" * 30_000, pr_title="t", pr_description="d")

    with (
        caplog.at_level(logging.WARNING, logger="adapters.llm"),
        pytest.raises(LLMInputTooLargeError) as error,
    ):
        await FallbackLLMGateway([make_provider(transport)], max_input_tokens=1000).review(big)

    assert transport.requests == []
    assert error.value.limit == 1000
    assert error.value.estimated_tokens > 1000
    assert "x" * 50 not in caplog.text


async def test_input_within_budget_is_reviewed() -> None:
    transport = RecordingTransport(replies(chat_response(review_json(VALID_FINDING))))

    result = await FallbackLLMGateway([make_provider(transport)], max_input_tokens=32000).review(
        REQUEST
    )

    assert len(result.findings) == 1


async def test_source_context_secret_is_redacted_in_provider_request() -> None:
    token = "ghp_" + "A" * 30
    rule_token = "ghp_" + "C" * 30
    request = ReviewRequest(
        diff_text="diff --git a/x b/x",
        pr_title="t",
        pr_description="d",
        rules_markdown=f"# Security\n- Do not expose {rule_token}.",
        context=(
            FileContext(
                path="app/users.py",
                blocks=(
                    ContextBlock(
                        kind=ContextBlockKind.ENCLOSING,
                        name="load_user",
                        start_line=40,
                        end_line=42,
                        text=f'def load_user():\n    token = "{token}"\n    return token',
                    ),
                ),
            ),
        ),
    )
    transport = RecordingTransport(replies(chat_response(review_json())))

    await FallbackLLMGateway([make_provider(transport)]).review(request)

    [outbound] = transport.requests
    messages = json.loads(outbound.content)["messages"]
    user_message = messages[1]["content"]
    assert token not in user_message
    assert rule_token not in user_message
    assert "[REDACTED:github_token]" in user_message
    assert "  42 |     return token" in user_message


async def test_rule_secret_is_redacted_in_typesafe_request() -> None:
    token = "ghp_" + "B" * 30
    transport = RecordingTransport(
        replies(
            httpx.Response(
                200,
                json={"model": "test", "answers": {"rule_1": {"noul": 0.0}}},
            )
        )
    )
    screen = JevRuleScreen(api_key="test-key", transport=transport)

    await screen.screen((Rule(id="R1", section="Security", text=f"Avoid {token}."),))

    [outbound] = transport.requests
    state = json.loads(outbound.content)["state"]
    assert token not in state
    assert "[REDACTED:github_token]" in state
