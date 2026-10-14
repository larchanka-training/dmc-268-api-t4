import time

import httpx

from adapters.llm.config import LLMSettings, ProviderSettings
from adapters.llm.gateway import ChatProvider, FallbackLLMGateway
from adapters.llm.keys import Clock, KeyPool
from adapters.llm.openai_compat import OpenAICompatibleProvider
from domain.ports import LLMGateway


def build_providers(
    settings: LLMSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock = time.monotonic,
) -> list[ChatProvider]:
    configured = [settings.primary]
    if settings.fallback is not None:
        configured.append(settings.fallback)
    return [
        _build_provider(provider, settings, transport=transport, clock=clock)
        for provider in configured
    ]


def build_gateway(
    settings: LLMSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock = time.monotonic,
) -> LLMGateway:
    providers = build_providers(settings, transport=transport, clock=clock)
    return FallbackLLMGateway(providers, max_input_tokens=settings.max_input_tokens)


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
        provider_order=provider.provider_order,
        timeout_seconds=settings.timeout_seconds,
        transport=transport,
    )
