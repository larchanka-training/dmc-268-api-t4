import logging
from collections.abc import Sequence
from typing import Any

import httpx

from adapters.llm.keys import DEFAULT_COOLDOWN_SECONDS, KeyPool, mask_key
from adapters.llm.prompt import ChatMessage
from adapters.resilience import Resilience
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


_TRANSIENT_REASONS = ("connection error", "HTTP 5")


def _is_transient(error: Exception) -> bool:
    """A dropped connection or a 5xx: fails fast, so worth repeating after a short wait.

    Not a timeout: each costs LLM_TIMEOUT_SECONDS, and repeating it would push one call past
    the job lease (LEASE_SECONDS); the gateway falls back to the next provider instead.
    """
    return isinstance(error, ProviderUnavailableError) and error.reason.startswith(
        _TRANSIENT_REASONS
    )


def _is_overload(error: Exception) -> bool:
    """The provider is unhealthy (feeds the breaker); a 4xx other than 429 is our own fault."""
    return isinstance(error, ProviderUnavailableError) and not (
        error.reason.startswith("HTTP 4") and not error.reason.startswith("HTTP 429")
    )


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
        resilience: Resilience | None = None,
    ) -> None:
        self._resilience = resilience
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

    async def complete(self, messages: Sequence[ChatMessage]) -> str:
        if self._resilience is None:
            return await self._complete_once(messages)
        return await self._resilience.call(
            lambda: self._complete_once(messages),
            is_failure=_is_overload,
            can_retry=_is_transient,
            open_error=lambda retry_in: ProviderUnavailableError(
                self._name, f"circuit open, next probe in {retry_in:.0f}s"
            ),
        )

    async def _complete_once(self, messages: Sequence[ChatMessage]) -> str:
        body = self._request_body(messages)
        async with httpx.AsyncClient(
            transport=self._transport, timeout=httpx.Timeout(self._timeout_seconds)
        ) as client:
            if self._key_pool is None:
                response = await self._post(client, body, key=None)
                return self._handle_response(response, key=None)
            for _ in range(len(self._key_pool)):
                key = self._key_pool.acquire()
                response = await self._post(client, body, key=key)
                if self._should_rotate_key(self._key_pool, response, key):
                    continue
                return self._handle_response(response, key)
        raise ProviderUnavailableError(self._name, "all API keys are rate-limited or rejected")

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

    def _should_rotate_key(self, pool: KeyPool, response: httpx.Response, key: str) -> bool:
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
            return True
        if response.status_code in _REJECTED_KEY_STATUSES:
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
            return True
        return False

    def _handle_response(self, response: httpx.Response, key: str | None) -> str:
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

    def _extract_content(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderUnavailableError(
                self._name, "malformed chat completion response"
            ) from None
        self._log_usage(payload, choice)
        if content is None:
            return ""
        if not isinstance(content, str):
            raise ProviderUnavailableError(self._name, "chat completion content is not text")
        return content

    def _log_usage(self, payload: Any, choice: Any) -> None:
        """Token counts and why the reply ended; a reply cut at max_tokens is not valid JSON."""
        usage = payload.get("usage") if isinstance(payload, dict) else None
        usage = usage if isinstance(usage, dict) else {}
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
