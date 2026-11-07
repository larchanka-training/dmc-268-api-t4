import random

import httpx
import pytest

from adapters.github.client import GitHubVCSClient
from adapters.github.publisher import GitHubReviewPublisher
from adapters.llm.openai_compat import OpenAICompatibleProvider
from adapters.resilience import Resilience
from domain.errors import ForgeUnavailableError, PrNotFoundError, ProviderUnavailableError
from domain.resilience import CircuitBreaker, RetryPolicy


class _Sleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _resilience(sleeper: _Sleeper, *, threshold: int = 5) -> Resilience:
    return Resilience(
        "test",
        RetryPolicy(max_attempts=3, base_seconds=1, cap_seconds=8, jitter_fraction=0),
        CircuitBreaker(failure_threshold=threshold, reset_seconds=30, clock=lambda: 0.0),
        sleep=sleeper,
        rng=random.Random(0),
    )


def _llm(handler: httpx.MockTransport, resilience: Resilience) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="p",
        base_url="http://llm.test/v1",
        model="m",
        key_pool=None,
        temperature=0,
        json_mode=False,
        timeout_seconds=5,
        max_tokens=10,
        transport=handler,
        resilience=resilience,
    )


def _ok() -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})


async def test_llm_retries_a_5xx_with_backoff_then_succeeds() -> None:
    answers = [httpx.Response(503), httpx.Response(502), _ok()]
    sleeper = _Sleeper()
    provider = _llm(httpx.MockTransport(lambda _: answers.pop(0)), _resilience(sleeper))

    assert await provider.complete([]) == "hi"
    assert sleeper.delays == [1, 2]


async def test_llm_does_not_retry_a_client_error() -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="bad")

    sleeper = _Sleeper()
    provider = _llm(httpx.MockTransport(handler), _resilience(sleeper))

    with pytest.raises(ProviderUnavailableError):
        await provider.complete([])
    assert len(calls) == 1
    assert sleeper.delays == []


async def test_llm_gives_up_after_max_attempts() -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    provider = _llm(httpx.MockTransport(handler), _resilience(_Sleeper()))

    with pytest.raises(ProviderUnavailableError):
        await provider.complete([])
    assert len(calls) == 3


async def test_llm_circuit_opens_and_stops_sending() -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    provider = _llm(httpx.MockTransport(handler), _resilience(_Sleeper(), threshold=2))

    with pytest.raises(ProviderUnavailableError):
        await provider.complete([])
    sent = len(calls)
    with pytest.raises(ProviderUnavailableError, match="circuit open"):
        await provider.complete([])
    assert len(calls) == sent


def _github(handler: httpx.MockTransport, resilience: Resilience) -> GitHubVCSClient:
    class _Auth:
        async def installation_token(self, installation_id: int) -> str:
            return "t"

    return GitHubVCSClient(  # type: ignore[arg-type]
        object(), _Auth(), transport=handler, base_url="http://gh.test", resilience=resilience
    )


async def test_github_get_retries_a_429_honouring_retry_after() -> None:
    answers = [httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200, json={})]
    sleeper = _Sleeper()
    client = _github(httpx.MockTransport(lambda _: answers.pop(0)), _resilience(sleeper))

    async with httpx.AsyncClient(transport=client._transport) as http:
        response = await client._get(http, "http://gh.test/x", {}, "step", 1)
    assert response.status_code == 200
    assert sleeper.delays == [3]


async def test_github_get_does_not_retry_a_404() -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404)

    client = _github(httpx.MockTransport(handler), _resilience(_Sleeper()))
    async with httpx.AsyncClient(transport=client._transport) as http:
        with pytest.raises(PrNotFoundError):
            await client._get(http, "http://gh.test/x", {}, "step", 1)
    assert len(calls) == 1


async def test_github_get_opens_the_circuit() -> None:
    client = _github(
        httpx.MockTransport(lambda _: httpx.Response(500)), _resilience(_Sleeper(), threshold=1)
    )
    async with httpx.AsyncClient(transport=client._transport) as http:
        with pytest.raises(ForgeUnavailableError):
            await client._get(http, "http://gh.test/x", {}, "step", 1)
        with pytest.raises(ForgeUnavailableError, match="circuit open"):
            await client._get(http, "http://gh.test/x", {}, "step", 1)


async def test_publisher_does_not_repeat_a_post_that_failed_with_5xx() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(502)

    publisher = GitHubReviewPublisher(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        resilience=_resilience(_Sleeper()),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://gh.test"
    ) as http:
        with pytest.raises(ForgeUnavailableError):
            await publisher._send(http, "POST", "/x", {})
        assert calls == ["POST"]
        calls.clear()
        with pytest.raises(ForgeUnavailableError):
            await publisher._send(http, "GET", "/x")
        assert calls == ["GET"] * 3
