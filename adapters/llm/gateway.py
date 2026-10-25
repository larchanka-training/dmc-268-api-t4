import dataclasses
import logging
import time
from collections.abc import Sequence
from typing import Protocol

from adapters.llm.config import DEFAULT_MAX_INPUT_TOKENS
from adapters.llm.prompt import (
    ChatMessage,
    build_correction_messages,
    build_messages,
    estimate_tokens,
)
from adapters.llm.reflection import VerificationReport
from adapters.llm.rule_screen import RuleScreenReport
from adapters.llm.rules import Rule, RuleSet, parse_rules
from adapters.llm.schema import InvalidReviewJSONError, ParsedReview, parse_review
from domain.errors import (
    AllProvidersFailedError,
    LLMGatewayError,
    LLMInputTooLargeError,
    LLMOutputError,
    ProviderUnavailableError,
)
from domain.models import Finding, ReviewResult, VerificationOutcome
from domain.ports import ReviewRequest

logger = logging.getLogger("adapters.llm")


class ChatProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def complete(self, messages: Sequence[ChatMessage]) -> str: ...


class FindingVerifier(Protocol):
    async def verify(
        self, request: ReviewRequest, rules: Sequence[Rule], findings: Sequence[Finding]
    ) -> VerificationReport: ...


class RuleScreen(Protocol):
    async def screen(self, rules: Sequence[Rule]) -> RuleScreenReport: ...


async def review_with_provider(
    provider: ChatProvider, request: ReviewRequest, rules: Sequence[Rule] = ()
) -> ReviewResult:
    """One provider's review: request, validate, and retry once if the reply is not JSON."""
    started = time.monotonic()
    rule_ids = frozenset(rule.id for rule in rules)
    messages = build_messages(request, rules)
    reply = await provider.complete(messages)
    try:
        parsed = parse_review(reply, rule_ids)
    except InvalidReviewJSONError as first_error:
        # The reason names the JSON error or an error count, never reply text.
        logger.warning(
            "llm.reply.rejected",
            extra={"provider": provider.name, "model": provider.model, "reason": str(first_error)},
        )
        reply = await provider.complete(build_correction_messages(messages, reply))
        parsed = _parse_final(provider, reply, rule_ids)

    if parsed.trimmed_findings:
        logger.warning(
            "llm.findings.trimmed",
            extra={
                "provider": provider.name,
                "model": provider.model,
                "kept": len(parsed.review.findings),
                "trimmed": parsed.trimmed_findings,
            },
        )
    if parsed.dropped_findings:
        logger.warning(
            "llm.findings.dropped",
            extra={
                "provider": provider.name,
                "model": provider.model,
                "dropped": parsed.dropped_findings,
            },
        )
    if parsed.unknown_rule_ids:
        logger.warning(
            "llm.rule_ids.cleared",
            extra={
                "provider": provider.name,
                "model": provider.model,
                "cleared": parsed.unknown_rule_ids,
            },
        )
    findings = parsed.review.to_domain()
    logger.info(
        "llm.review.finished",
        extra={
            "provider": provider.name,
            "model": provider.model,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "findings": len(findings),
        },
    )
    return ReviewResult(
        summary=parsed.review.summary,
        findings=findings,
        provider=provider.name,
        model=provider.model,
    )


def _parse_final(provider: ChatProvider, reply: str, rule_ids: frozenset[str]) -> ParsedReview:
    try:
        return parse_review(reply, rule_ids)
    except InvalidReviewJSONError as exc:
        raise LLMOutputError(provider.name, f"{exc} after a corrective retry") from None


class FallbackLLMGateway:
    """domain.ports.LLMGateway: tries providers in order until one returns a review."""

    def __init__(
        self,
        providers: Sequence[ChatProvider],
        *,
        max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
        verifier: FindingVerifier | None = None,
        rule_screen: RuleScreen | None = None,
    ) -> None:
        if not providers:
            raise ValueError("FallbackLLMGateway needs at least one provider")
        self._providers = tuple(providers)
        self._max_input_tokens = max_input_tokens
        self._verifier = verifier
        self._rule_screen = rule_screen

    async def review(self, request: ReviewRequest) -> ReviewResult:
        rule_set = parse_rules(request.rules_markdown)
        screen = await self._screen_rules(rule_set.rules)
        _log_rules(rule_set, screen)
        rules = screen.rules
        estimated = estimate_tokens(build_messages(request, rules))
        if estimated > self._max_input_tokens:
            too_large = LLMInputTooLargeError(estimated, self._max_input_tokens)
            logger.warning(
                "llm.input.too_large",
                extra={"input_tokens": estimated, "max_input_tokens": self._max_input_tokens},
            )
            raise too_large
        result = await self._first_pass(request, rules)
        if self._verifier is None:
            return result
        try:
            report = await self._verifier.verify(request, rules, result.findings)
        except Exception as exc:
            # The verifier must never fail the review: publish the first pass as it is.
            logger.warning("llm.verifier.failed", extra={"error_type": type(exc).__name__})
            return dataclasses.replace(result, verification=VerificationOutcome.FAILED)
        return dataclasses.replace(
            result,
            findings=report.findings,
            verification=report.outcome,
            rejected_by_verifier=report.rejected,
        )

    async def _screen_rules(self, rules: Sequence[Rule]) -> RuleScreenReport:
        unchanged = tuple(rules)
        if not rules:
            return RuleScreenReport(VerificationOutcome.SKIPPED, unchanged)
        if self._rule_screen is None:
            # No TypeSafe key or LLM_REFLECTION_ENABLED=false: patterns only.
            logger.warning("llm.rule_screen.disabled", extra={"rules": len(rules)})
            return RuleScreenReport(VerificationOutcome.DISABLED, unchanged)
        try:
            return await self._rule_screen.screen(rules)
        except Exception as exc:
            # The screen must never fail the review: fall back to the pattern filter alone.
            logger.warning(
                "llm.rule_screen.failed",
                extra={"error_type": type(exc).__name__, "rules": len(rules)},
            )
            return RuleScreenReport(VerificationOutcome.FAILED, unchanged)

    async def _first_pass(self, request: ReviewRequest, rules: Sequence[Rule]) -> ReviewResult:
        causes: list[LLMGatewayError] = []
        for index, provider in enumerate(self._providers):
            try:
                return await review_with_provider(provider, request, rules)
            except (ProviderUnavailableError, LLMOutputError) as exc:
                causes.append(exc)
                if index + 1 < len(self._providers):
                    logger.warning(
                        "llm.provider.fallback",
                        extra={
                            "provider": provider.name,
                            "next_provider": self._providers[index + 1].name,
                            "reason": _log_reason(exc),
                        },
                    )
        error = AllProvidersFailedError(causes)
        # A warning, not an error: the worker retries the job, and only a job that
        # ends FAILED becomes a Sentry event (docs/observability.md § Levels).
        logger.warning(
            "llm.call.failed",
            extra={"reason": "; ".join(_log_reason(cause) for cause in causes)},
        )
        raise error


def _log_reason(error: LLMGatewayError) -> str:
    """What a log line may say about a provider failure: the provider and the
    reason, never a ProviderUnavailableError's ``detail`` (the start of the
    response body), which may echo the request."""
    if isinstance(error, ProviderUnavailableError | LLMOutputError):
        return f"{error.provider}: {error.reason}"
    return type(error).__name__


def _log_rules(rule_set: RuleSet, screen: RuleScreenReport) -> None:
    """Counters only: rule texts come from the repository and never reach the logs."""
    dropped = rule_set.dropped_suspicious + screen.dropped + rule_set.dropped_over_limit
    if not screen.rules and not dropped:
        return
    logger.log(
        logging.WARNING if dropped else logging.INFO,
        "llm.rules.screened",
        extra={
            "kept": len(screen.rules),
            "dropped_pattern": rule_set.dropped_suspicious,
            "dropped_screen": screen.dropped,
            "screen": str(screen.outcome),
            "dropped_over_limit": rule_set.dropped_over_limit,
        },
    )
