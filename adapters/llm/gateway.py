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
        logger.warning(
            "provider=%s model=%s reply rejected (%s), retrying once",
            provider.name,
            provider.model,
            first_error,
        )
        reply = await provider.complete(build_correction_messages(messages, reply))
        parsed = _parse_final(provider, reply, rule_ids)

    if parsed.trimmed_findings:
        logger.warning(
            "provider=%s model=%s kept the %d most severe finding(s), trimmed %d over the limit",
            provider.name,
            provider.model,
            len(parsed.review.findings),
            parsed.trimmed_findings,
        )
    if parsed.dropped_findings:
        logger.warning(
            "provider=%s model=%s dropped %d finding(s) that failed schema validation",
            provider.name,
            provider.model,
            parsed.dropped_findings,
        )
    if parsed.unknown_rule_ids:
        logger.warning(
            "provider=%s model=%s cleared rule_id on %d finding(s) citing no listed rule",
            provider.name,
            provider.model,
            parsed.unknown_rule_ids,
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
            logger.warning("%s", too_large)
            raise too_large
        result = await self._first_pass(request, rules)
        if self._verifier is None:
            return result
        try:
            report = await self._verifier.verify(request, rules, result.findings)
        except Exception as exc:
            # The verifier must never fail the review: publish the first pass as it is.
            logger.warning("verifier raised %s, keeping the first pass", type(exc).__name__)
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
            logger.warning(
                "rule screen disabled (no TypeSafe key or LLM_REFLECTION_ENABLED=false), "
                "%d rule(s) checked by patterns only",
                len(rules),
            )
            return RuleScreenReport(VerificationOutcome.DISABLED, unchanged)
        try:
            return await self._rule_screen.screen(rules)
        except Exception as exc:
            # The screen must never fail the review: fall back to the pattern filter alone.
            logger.warning(
                "rule screen raised %s, %d rule(s) checked by patterns only",
                type(exc).__name__,
                len(rules),
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
                        "%s; falling back to provider=%s", exc, self._providers[index + 1].name
                    )
        error = AllProvidersFailedError(causes)
        logger.error("%s", error)
        raise error


def _log_rules(rule_set: RuleSet, screen: RuleScreenReport) -> None:
    """Counters only: rule texts come from the repository and never reach the logs."""
    dropped = rule_set.dropped_suspicious + screen.dropped + rule_set.dropped_over_limit
    if not screen.rules and not dropped:
        return
    logger.log(
        logging.WARNING if dropped else logging.INFO,
        "repository rules: kept %d, dropped %d by pattern, %d by JEV (screen %s), "
        "%d over the limits",
        len(screen.rules),
        rule_set.dropped_suspicious,
        screen.dropped,
        screen.outcome,
        rule_set.dropped_over_limit,
    )
