import time

import httpx

from adapters.llm.config import LLMSettings, ProviderSettings
from adapters.llm.gateway import FallbackLLMGateway
from adapters.llm.keys import Clock, KeyPool
from adapters.llm.openai_compat import OpenAICompatibleProvider
from domain.ports import LLMGateway


def build_gateway(
    settings: LLMSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock = time.monotonic,
) -> LLMGateway:
    configured = [settings.primary]
    if settings.fallback is not None:
        configured.append(settings.fallback)
    providers = [
        _build_provider(provider, settings, transport=transport, clock=clock)
        for provider in configured
    ]
    return FallbackLLMGateway(providers)


def _build_provider(
    provider: ProviderSettings,
    settings: LLMSettings,
    *,
    transport: httpx.AsyncBaseTransport | None,
    clock: Clock,
) -> OpenAICompatibleProvider:
    key_pool = KeyPool(provider.name, provider.api_keys, clock) if provider.api_keys else None
    return OpenAICompatibleProvider(
        name=provider.name,
        base_url=provider.base_url,
        model=provider.model,
        key_pool=key_pool,
        temperature=settings.temperature,
        json_mode=settings.json_mode,
        max_tokens=settings.max_tokens,
        timeout_seconds=settings.timeout_seconds,
        transport=transport,
    )
