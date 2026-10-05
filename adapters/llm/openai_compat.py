import logging
from collections.abc import Sequence
from typing import Any

import httpx

from adapters.llm.keys import DEFAULT_COOLDOWN_SECONDS, KeyPool, mask_key
from adapters.llm.prompt import ChatMessage
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

    async def complete(self, messages: Sequence[ChatMessage]) -> str:
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
                "provider=%s key=%s rate-limited (HTTP 429), cooldown %gs",
                self._name,
                mask_key(key),
                seconds,
            )
            return True
        if response.status_code in _REJECTED_KEY_STATUSES:
            pool.disable(key)
            logger.warning(
                "provider=%s key=%s rejected (HTTP %d), disabled until restart",
                self._name,
                mask_key(key),
                response.status_code,
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
            raise ProviderUnavailableError(self._name, f"HTTP {status}: {preview}")
        if status != 200:
            raise ProviderUnavailableError(self._name, f"unexpected HTTP {status}")
        return self._extract_content(response)

    def _extract_content(self, response: httpx.Response) -> str:
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderUnavailableError(
                self._name, "malformed chat completion response"
            ) from None
        if content is None:
            return ""
        if not isinstance(content, str):
            raise ProviderUnavailableError(self._name, "chat completion content is not text")
        return content
