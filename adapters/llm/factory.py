import time
from collections.abc import Callable

import httpx

from adapters.llm.config import LLMSettings, ProviderSettings
from adapters.llm.gateway import ChatProvider, FallbackLLMGateway
from adapters.llm.keys import Clock, KeyPool
from adapters.llm.openai_compat import OpenAICompatibleProvider
from adapters.llm.reflection import JevVerifier
from adapters.llm.rule_screen import JevRuleScreen
from domain.ports import LLMGateway


def build_gateway(
    settings: LLMSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock = time.monotonic,
    wrap_provider: Callable[[ChatProvider], ChatProvider] | None = None,
) -> LLMGateway:
    """Wire the review pipeline; wrap_provider lets the eval harness observe each provider."""
    configured = [settings.primary]
    if settings.fallback is not None:
        configured.append(settings.fallback)
    providers: list[ChatProvider] = [
        _build_provider(provider, settings, transport=transport, clock=clock)
        for provider in configured
    ]
    if wrap_provider is not None:
        providers = [wrap_provider(provider) for provider in providers]
    return FallbackLLMGateway(
        providers,
        max_input_tokens=settings.max_input_tokens,
        verifier=_build_verifier(settings, transport=transport),
        rule_screen=_build_rule_screen(settings, transport=transport),
    )


def _build_rule_screen(
    settings: LLMSettings, *, transport: httpx.AsyncBaseTransport | None
) -> JevRuleScreen | None:
    reflection = settings.reflection
    if not reflection.is_active:
        return None
    return JevRuleScreen(
        api_key=reflection.api_key,
        base_url=reflection.base_url,
        model=reflection.model,
        drop_threshold=reflection.rules_screen_threshold,
        timeout_seconds=reflection.timeout_seconds,
        transport=transport,
    )


def _build_verifier(
    settings: LLMSettings, *, transport: httpx.AsyncBaseTransport | None
) -> JevVerifier | None:
    reflection = settings.reflection
    if not reflection.is_active:
        return None
    return JevVerifier(
        api_key=reflection.api_key,
        base_url=reflection.base_url,
        model=reflection.model,
        keep_threshold=reflection.keep_threshold,
        timeout_seconds=reflection.timeout_seconds,
        max_input_tokens=settings.max_input_tokens,
        transport=transport,
    )


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
