import json

import httpx
import pytest

from adapters.llm.openai_compat import parse_retry_after
from adapters.llm.prompt import ChatMessage
from domain.errors import ProviderUnavailableError
from tests.conftest import (
    FAKE_KEY_A,
    FAKE_KEY_B,
    FakeClock,
    RecordingTransport,
    chat_response,
    make_provider,
    replies,
)

MESSAGES: list[ChatMessage] = [{"role": "user", "content": "review this"}]


def auth_header(request: httpx.Request) -> str | None:
    return request.headers.get("Authorization")


async def test_429_rotates_to_next_key_and_cools_down_the_first(clock: FakeClock) -> None:
    transport = RecordingTransport(
        replies(
            httpx.Response(429, headers={"Retry-After": "30"}),
            chat_response("ok-from-b"),
            chat_response("ok-from-b-again"),
            chat_response("ok-from-a"),
        )
    )
    provider = make_provider(transport, keys=(FAKE_KEY_A, FAKE_KEY_B), clock=clock)

    assert await provider.complete(MESSAGES) == "ok-from-b"
    assert [auth_header(r) for r in transport.requests] == [
        f"Bearer {FAKE_KEY_A}",
        f"Bearer {FAKE_KEY_B}",
    ]

    clock.now += 29
    await provider.complete(MESSAGES)
    assert auth_header(transport.requests[-1]) == f"Bearer {FAKE_KEY_B}"

    clock.now += 1
    await provider.complete(MESSAGES)
    assert auth_header(transport.requests[-1]) == f"Bearer {FAKE_KEY_A}"


async def test_401_disables_key_permanently(clock: FakeClock) -> None:
    transport = RecordingTransport(
        replies(
            httpx.Response(401, json={"error": "invalid key"}),
            chat_response("ok"),
            chat_response("ok"),
            chat_response("ok"),
        )
    )
    provider = make_provider(transport, keys=(FAKE_KEY_A, FAKE_KEY_B), clock=clock)

    assert await provider.complete(MESSAGES) == "ok"
    clock.now += 10**6
    await provider.complete(MESSAGES)
    await provider.complete(MESSAGES)

    assert [auth_header(r) for r in transport.requests] == [
        f"Bearer {FAKE_KEY_A}",
        *[f"Bearer {FAKE_KEY_B}"] * 3,
    ]


async def test_all_keys_rate_limited_raises_provider_unavailable(clock: FakeClock) -> None:
    transport = RecordingTransport(
        replies(*[httpx.Response(429, headers={"Retry-After": "0"}) for _ in range(2)])
    )
    provider = make_provider(transport, keys=(FAKE_KEY_A, FAKE_KEY_B), clock=clock)

    with pytest.raises(ProviderUnavailableError, match="rate-limited"):
        await provider.complete(MESSAGES)
    assert len(transport.requests) == 2


async def test_all_keys_in_cooldown_fails_without_request(clock: FakeClock) -> None:
    transport = RecordingTransport(replies(*[httpx.Response(429) for _ in range(2)]))
    provider = make_provider(transport, keys=(FAKE_KEY_A, FAKE_KEY_B), clock=clock)
    with pytest.raises(ProviderUnavailableError):
        await provider.complete(MESSAGES)

    with pytest.raises(ProviderUnavailableError, match="rate-limited"):
        await provider.complete(MESSAGES)
    assert len(transport.requests) == 2


async def test_keyless_provider_sends_no_authorization_header() -> None:
    transport = RecordingTransport(replies(chat_response("ok")))
    provider = make_provider(transport, name="ollama", keys=())

    await provider.complete(MESSAGES)

    assert auth_header(transport.requests[0]) is None


async def test_keyed_provider_sends_bearer_to_chat_completions() -> None:
    transport = RecordingTransport(replies(chat_response("ok")))

    await make_provider(transport).complete(MESSAGES)

    request = transport.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://llm.example.test/api/v1/chat/completions"
    assert auth_header(request) == f"Bearer {FAKE_KEY_A}"


@pytest.mark.parametrize("json_mode", [True, False])
async def test_request_body_fields(json_mode: bool) -> None:
    transport = RecordingTransport(replies(chat_response("ok")))

    await make_provider(transport, json_mode=json_mode).complete(MESSAGES)

    body = json.loads(transport.requests[0].read())
    assert body["model"] == "eurouter-model"
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 1024
    assert body["messages"] == MESSAGES
    if json_mode:
        assert body["response_format"] == {"type": "json_object"}
    else:
        assert "response_format" not in body
    assert "provider" not in body


async def test_provider_order_pins_upstream_providers() -> None:
    transport = RecordingTransport(replies(chat_response("ok")))

    await make_provider(transport, provider_order=("scaleway", "ovhcloud")).complete(MESSAGES)

    body = json.loads(transport.requests[0].read())
    assert body["provider"] == {"order": ["scaleway", "ovhcloud"], "allow_fallbacks": False}


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (httpx.Response(503), "HTTP 503"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectError("refused"), "connection error"),
        (httpx.Response(200, json={"unexpected": True}), "malformed"),
    ],
)
async def test_provider_failures_become_provider_unavailable(
    failure: httpx.Response | Exception, reason: str
) -> None:
    provider = make_provider(RecordingTransport(replies(failure)))

    with pytest.raises(ProviderUnavailableError, match=reason) as excinfo:
        await provider.complete(MESSAGES)
    assert excinfo.value.provider == "eurouter"


async def test_client_error_includes_code_and_truncated_body() -> None:
    body = "model 'gpt-nope' not found " + "x" * 500
    provider = make_provider(RecordingTransport(replies(httpx.Response(404, text=body))))

    with pytest.raises(ProviderUnavailableError) as excinfo:
        await provider.complete(MESSAGES)

    assert "HTTP 404: model 'gpt-nope' not found" in str(excinfo.value)
    assert excinfo.value.reason == f"HTTP 404: {body[:200]}"


async def test_null_content_is_returned_as_empty_text() -> None:
    provider = make_provider(RecordingTransport(replies(chat_response(None))))

    assert await provider.complete(MESSAGES) == ""


@pytest.mark.parametrize(
    ("header", "seconds"),
    [("30", 30.0), (None, 60.0), ("Wed, 21 Oct 2026 07:28:00 GMT", 60.0), ("-5", 0.0)],
)
def test_parse_retry_after(header: str | None, seconds: float) -> None:
    assert parse_retry_after(header) == seconds
