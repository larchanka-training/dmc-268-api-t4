import logging
import time
from collections.abc import Sequence
from typing import Protocol

from adapters.llm.prompt import ChatMessage, build_correction_messages, build_messages
from adapters.llm.schema import InvalidReviewJSONError, ParsedReview, parse_review
from domain.errors import (
    AllProvidersFailedError,
    LLMGatewayError,
    LLMOutputError,
    ProviderUnavailableError,
)
from domain.models import ReviewResult
from domain.ports import ReviewRequest

logger = logging.getLogger("adapters.llm")


class ChatProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def complete(self, messages: Sequence[ChatMessage]) -> str: ...


async def review_with_provider(provider: ChatProvider, request: ReviewRequest) -> ReviewResult:
    """One provider's review: request, validate, and retry once if the reply is not JSON."""
    started = time.monotonic()
    messages = build_messages(request)
    reply = await provider.complete(messages)
    try:
        parsed = parse_review(reply)
    except InvalidReviewJSONError as first_error:
        logger.warning(
            "provider=%s model=%s reply rejected (%s), retrying once",
            provider.name,
            provider.model,
            first_error,
        )
        reply = await provider.complete(build_correction_messages(messages, reply))
        parsed = _parse_final(provider, reply)

    if parsed.dropped_findings:
        logger.warning(
            "provider=%s model=%s dropped %d finding(s) that failed schema validation",
            provider.name,
            provider.model,
            parsed.dropped_findings,
        )
    findings = parsed.review.to_domain()
    logger.info(
        "provider=%s model=%s duration=%.2fs findings=%d",
        provider.name,
        provider.model,
        time.monotonic() - started,
        len(findings),
    )
    return ReviewResult(
        summary=parsed.review.summary,
        findings=findings,
        provider=provider.name,
        model=provider.model,
    )


def _parse_final(provider: ChatProvider, reply: str) -> ParsedReview:
    try:
        return parse_review(reply)
    except InvalidReviewJSONError as exc:
        raise LLMOutputError(provider.name, f"{exc} after a corrective retry") from None


class FallbackLLMGateway:
    """domain.ports.LLMGateway: tries providers in order until one returns a review."""

    def __init__(self, providers: Sequence[ChatProvider]) -> None:
        if not providers:
            raise ValueError("FallbackLLMGateway needs at least one provider")
        self._providers = tuple(providers)

    async def review(self, request: ReviewRequest) -> ReviewResult:
        causes: list[LLMGatewayError] = []
        for index, provider in enumerate(self._providers):
            try:
                return await review_with_provider(provider, request)
            except (ProviderUnavailableError, LLMOutputError) as exc:
                causes.append(exc)
                if index + 1 < len(self._providers):
                    logger.warning(
                        "%s; falling back to provider=%s", exc, self._providers[index + 1].name
                    )
        error = AllProvidersFailedError(causes)
        logger.error("%s", error)
        raise error
