import logging

import httpx
import pytest

from adapters.llm.config import LLMSettings
from adapters.llm.gateway import FallbackLLMGateway
from domain.errors import AllProvidersFailedError
from domain.ports import ReviewRequest
from tests.conftest import FakeClock, RecordingTransport, make_provider, replies

KEY_ONE = "sk-test-leakcheck-0000000001"
KEY_TWO = "sk-test-leakcheck-0000000002"
KEY_THREE = "sk-test-leakcheck-0000000003"
ALL_KEYS = (KEY_ONE, KEY_TWO, KEY_THREE)


async def test_keys_never_appear_in_logs_or_errors(
    caplog: pytest.LogCaptureFixture, clock: FakeClock
) -> None:
    transport = RecordingTransport(
        replies(
            httpx.Response(429, headers={"Retry-After": "5"}),
            httpx.Response(401, text=f"invalid api key {KEY_TWO}"),
            httpx.Response(400, text=f"bad request for key {KEY_THREE}: model missing"),
        )
    )
    primary = make_provider(transport, keys=ALL_KEYS, clock=clock)
    fallback = make_provider(
        RecordingTransport(replies(httpx.Response(500, text=KEY_ONE))), name="ollama", keys=()
    )
    request = ReviewRequest(diff_text="+print(1)", pr_title="t", pr_description="d")

    with (
        caplog.at_level(logging.DEBUG, logger="adapters.llm"),
        pytest.raises(AllProvidersFailedError) as excinfo,
    ):
        await FallbackLLMGateway([primary, fallback]).review(request)

    rendered = [str(excinfo.value), repr(excinfo.value), caplog.text]
    rendered += [str(cause) for cause in excinfo.value.causes]
    for key in ALL_KEYS:
        assert all(key not in text for text in rendered), key
    assert "sk-…0001" in caplog.text
    assert "sk-…0003" in str(excinfo.value)


def test_settings_repr_hides_keys() -> None:
    settings = LLMSettings.from_env(
        {"LLM_PRIMARY_API_KEYS": ",".join(ALL_KEYS), "LLM_PRIMARY_MODEL": "m"}
    )

    for key in ALL_KEYS:
        assert key not in repr(settings)
        assert key not in str(settings)
