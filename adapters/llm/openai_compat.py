import logging
import time
from collections.abc import Sequence
from typing import Any

import httpx
import sentry_sdk

from adapters.llm.keys import DEFAULT_COOLDOWN_SECONDS, KeyPool, mask_key
from adapters.llm.prompt import ChatMessage
from adapters.observability import metrics
from domain.errors import ProviderUnavailableError

logger = logging.getLogger("adapters.llm")

ERROR_BODY_PREVIEW_CHARS = 200
_REJECTED_KEY_STATUSES = frozenset({401, 403})


def parse_retry_after(value: str | None) -> float:
    """Seconds from a Retry-After header; HTTP-date and garbage fall back to the default."""
    if value is None:
        return DEFAULT_COOLDOWN_SECONDS
    try:
        return max(float(value.strip()), 0.0)
    except ValueError:
        return DEFAULT_COOLDOWN_SECONDS


class OpenAICompatibleProvider:
    """POST {base_url}/chat/completions; serves both Eurouter and Ollama's /v1 API."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        model: str,
        key_pool: KeyPool | None,
        temperature: float,
        json_mode: bool,
        timeout_seconds: float,
        max_tokens: int,
        provider_order: tuple[str, ...] = (),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._name = name
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model
        self._key_pool = key_pool
        self._temperature = temperature
        self._json_mode = json_mode
        self._max_tokens = max_tokens
        self._provider_order = provider_order
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model

    async def complete(self, messages: Sequence[ChatMessage], *, purpose: str = "review") -> str:
        """The reply text. ``purpose`` (review, correction, summary) labels the request's
        metrics and span (docs/observability.md § Review pipeline)."""
        body = self._request_body(messages)
        async with httpx.AsyncClient(
            transport=self._transport, timeout=httpx.Timeout(self._timeout_seconds)
        ) as client:
            if self._key_pool is None:
                reply = await self._attempt(client, body, None, purpose)
                assert reply is not None  # no pool, no rotation
                return reply
            for _ in range(len(self._key_pool)):
                reply = await self._attempt(client, body, self._key_pool.acquire(), purpose)
                if reply is not None:
                    return reply
        raise ProviderUnavailableError(self._name, "all API keys are rate-limited or rejected")

    async def _attempt(
        self, client: httpx.AsyncClient, body: dict[str, Any], key: str | None, purpose: str
    ) -> str | None:
        """One HTTP request as one ``llm.attempt`` span and one counted request; None when
        the key was set aside and the next one should be tried. Counts and names only:
        the messages and the reply never reach the span."""
        with sentry_sdk.start_span(op="llm.attempt", name=f"{self._name} {self._model}") as span:
            for name, value in (("provider", self._name), ("model", self._model)):
                span.set_data(name, value)
            span.set_data("purpose", purpose)
            outcome = "error"
            started = time.monotonic()
            try:
                response = await self._post(client, body, key=key)
                if key is not None and self._key_pool is not None:
                    rotated = self._rotation(response)
                    if rotated is not None:
                        outcome = rotated
                        self._set_key_aside(self._key_pool, response, key)
                        metrics.count_key_rotation(self._name)
                        return None
                reply, usage = self._handle_response(response, key)
                outcome = "ok"
                metrics.observe_llm_latency(
                    self._name, self._model, purpose, time.monotonic() - started
                )
                metrics.count_llm_tokens(
                    self._name,
                    self._model,
                    purpose,
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                )
                span.set_data("input_tokens", usage.get("prompt_tokens"))
                span.set_data("output_tokens", usage.get("completion_tokens"))
                return reply
            finally:
                span.set_data("outcome", outcome)
                metrics.count_llm_request(self._name, self._model, purpose, outcome)

    def _request_body(self, messages: Sequence[ChatMessage]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": list(messages),
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        if self._json_mode:
            body["response_format"] = {"type": "json_object"}
        if self._provider_order:
            body["provider"] = {"order": list(self._provider_order), "allow_fallbacks": False}
        return body

    async def _post(
        self, client: httpx.AsyncClient, body: dict[str, Any], *, key: str | None
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            return await client.post(self._url, json=body, headers=headers)
        except httpx.TimeoutException:
            raise ProviderUnavailableError(
                self._name, f"timeout after {self._timeout_seconds:g}s"
            ) from None
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                self._name, f"connection error ({type(exc).__name__})"
            ) from None

    @staticmethod
    def _rotation(response: httpx.Response) -> str | None:
        """The outcome when this response sets the key aside: rate_limited or key_rejected."""
        if response.status_code == 429:
            return "rate_limited"
        if response.status_code in _REJECTED_KEY_STATUSES:
            return "key_rejected"
        return None

    def _set_key_aside(self, pool: KeyPool, response: httpx.Response, key: str) -> None:
        if response.status_code == 429:
            seconds = parse_retry_after(response.headers.get("Retry-After"))
            pool.cool_down(key, seconds)
            logger.warning(
                "llm.key.rate_limited",
                extra={
                    "provider": self._name,
                    "key_masked": mask_key(key),
                    "cooldown_seconds": seconds,
                },
            )
            return
        pool.disable(key)
        # Disabled until restart.
        logger.warning(
            "llm.key.rejected",
            extra={
                "provider": self._name,
                "key_masked": mask_key(key),
                "http.status": response.status_code,
            },
        )

    def _handle_response(
        self, response: httpx.Response, key: str | None
    ) -> tuple[str, dict[str, Any]]:
        """The reply text and the provider's usage block (empty when it sent none)."""
        status = response.status_code
        if status >= 500:
            raise ProviderUnavailableError(self._name, f"HTTP {status}")
        if status >= 400:
            body_text = response.text.replace(key, mask_key(key)) if key else response.text
            preview = body_text[:ERROR_BODY_PREVIEW_CHARS]
            raise ProviderUnavailableError(self._name, f"HTTP {status}", detail=preview)
        if status != 200:
            raise ProviderUnavailableError(self._name, f"unexpected HTTP {status}")
        return self._extract_content(response)

    def _extract_content(self, response: httpx.Response) -> tuple[str, dict[str, Any]]:
        try:
            payload = response.json()
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderUnavailableError(
                self._name, "malformed chat completion response"
            ) from None
        usage = self._log_usage(payload, choice)
        if content is None:
            return "", usage
        if not isinstance(content, str):
            raise ProviderUnavailableError(self._name, "chat completion content is not text")
        return content, usage

    def _log_usage(self, payload: Any, choice: Any) -> dict[str, Any]:
        """Token counts and why the reply ended; a reply cut at max_tokens is not valid JSON.
        Returns the usage block."""
        raw = payload.get("usage") if isinstance(payload, dict) else None
        usage: dict[str, Any] = raw if isinstance(raw, dict) else {}
        finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        if finish_reason == "length":
            logger.warning(
                "llm.reply.truncated",
                extra={
                    "provider": self._name,
                    "model": self._model,
                    "max_tokens": self._max_tokens,
                    "output_tokens": usage.get("completion_tokens"),
                },
            )
        logger.info(
            "llm.response.usage",
            extra={
                "provider": self._name,
                "model": self._model,
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
                "finish_reason": finish_reason[:32] if isinstance(finish_reason, str) else None,
            },
        )
        return usage
