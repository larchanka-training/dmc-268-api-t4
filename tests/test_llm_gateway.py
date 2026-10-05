import logging

import httpx
import pytest

from adapters.llm.config import LLMSettings, ProviderSettings
from adapters.llm.factory import build_gateway
from adapters.llm.gateway import FallbackLLMGateway
from domain.errors import (
    AllProvidersFailedError,
    LLMInputTooLargeError,
    LLMOutputError,
    ProviderUnavailableError,
)
from domain.ports import ReviewRequest
from tests.conftest import (
    FAKE_KEY_A,
    FAKE_KEY_B,
    VALID_FINDING,
    RecordingTransport,
    chat_response,
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
    assert "falling back to provider=ollama" in caplog.text
    assert "provider=ollama model=ollama-model" in caplog.text


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
    assert any(record.levelno == logging.ERROR for record in caplog.records)


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
