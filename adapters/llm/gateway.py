import asyncio
import dataclasses
import logging
import time
from collections.abc import Sequence
from typing import Protocol

import sentry_sdk

from adapters.llm.config import (
    DEFAULT_MAX_INPUT_TOKENS,
    DEFAULT_PROMPT_VERSION,
    DEFAULT_REVIEW_SCOPE,
    ReviewScope,
)
from adapters.llm.prompt import (
    ChatMessage,
    FileReview,
    build_correction_messages,
    build_messages,
    build_summary_messages,
    estimate_tokens,
)
from adapters.llm.reflection import VerificationReport
from adapters.llm.rule_screen import RuleScreenReport
from adapters.llm.rules import Rule, RuleSet, parse_rules
from adapters.llm.schema import (
    CHECKLIST_MAX_ITEMS,
    InvalidReviewJSONError,
    ParsedReview,
    ParsedSummary,
    parse_review,
    parse_summary,
)
from domain.diff import DiffLineKind, parse_diff, split_by_file
from domain.errors import (
    AllProvidersFailedError,
    DiffFormatError,
    LLMGatewayError,
    LLMInputTooLargeError,
    LLMOutputError,
    ProviderUnavailableError,
)
from domain.models import RISK_ORDER, Finding, ReviewResult, VerificationOutcome
from domain.ports import ReviewRequest

logger = logging.getLogger("adapters.llm")

# Prompt versions whose output schema carries risk and checklist (schema v2, issue #55).
PROMPTS_WITH_RISK = frozenset({"v2"})

# Per-file review: calls in flight at once, so a 30-file pull request does not open 30
# connections to one provider key.
FILE_CALLS_CONCURRENCY = 4


class ChatProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def complete(self, messages: Sequence[ChatMessage], *, purpose: str = "review") -> str:
        """``purpose`` (review, correction, summary) labels the request's metrics and span."""
        ...


class FindingVerifier(Protocol):
    async def verify(
        self, request: ReviewRequest, rules: Sequence[Rule], findings: Sequence[Finding]
    ) -> VerificationReport: ...


class RuleScreen(Protocol):
    async def screen(self, rules: Sequence[Rule]) -> RuleScreenReport: ...


async def review_with_provider(
    provider: ChatProvider,
    request: ReviewRequest,
    rules: Sequence[Rule] = (),
    *,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
) -> ReviewResult:
    """One provider's review: request, validate, and retry once if the reply is not JSON."""
    started = time.monotonic()
    rule_ids = frozenset(rule.id for rule in rules)
    messages = build_messages(request, rules, version=prompt_version)
    reply = await provider.complete(messages)
    try:
        parsed = parse_review(reply, rule_ids)
    except InvalidReviewJSONError as first_error:
        # The reason names the JSON error or an error count, never reply text.
        logger.warning(
            "llm.reply.rejected",
            extra={"provider": provider.name, "model": provider.model, "reason": str(first_error)},
        )
        reply = await provider.complete(
            build_correction_messages(messages, reply), purpose="correction"
        )
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
                # Which schema field failed, and how often ({"severity": 1, "line": 2}):
                # FindingOut's field names, never text from the reply.
                "dropped_fields": parsed.dropped_fields,
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
    if prompt_version in PROMPTS_WITH_RISK:
        _log_summary_fields(provider, parsed)
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
        risk=parsed.review.risk,
        checklist=parsed.review.checklist,
    )


def _log_summary_fields(provider: ChatProvider, parsed: ParsedReview) -> None:
    """Counters only: a missing or bad risk falls back to the rule-based one when publishing."""
    risk_problem = (
        "invalid" if parsed.risk_invalid else "missing" if parsed.review.risk is None else None
    )
    if risk_problem or parsed.checklist_invalid_items or parsed.checklist_trimmed:
        logger.warning(
            "llm.summary_fields.invalid",
            extra={
                "provider": provider.name,
                "model": provider.model,
                "risk_problem": risk_problem,
                "checklist_invalid": parsed.checklist_invalid_items,
                "checklist_trimmed": parsed.checklist_trimmed,
            },
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
        prompt_version: str = DEFAULT_PROMPT_VERSION,
        review_scope: ReviewScope = DEFAULT_REVIEW_SCOPE,
    ) -> None:
        if not providers:
            raise ValueError("FallbackLLMGateway needs at least one provider")
        self._providers = tuple(providers)
        self._max_input_tokens = max_input_tokens
        self._verifier = verifier
        self._rule_screen = rule_screen
        self._prompt_version = prompt_version
        self._review_scope = review_scope

    async def review(self, request: ReviewRequest) -> ReviewResult:
        rule_set = parse_rules(request.rules_markdown)
        screen = await self._screen_rules(rule_set.rules)
        _log_rules(rule_set, screen)
        rules = screen.rules
        parts = self._split(request)
        estimated = max(
            estimate_tokens(build_messages(part, rules, version=self._prompt_version))
            for part in parts
        )
        if estimated > self._max_input_tokens:
            too_large = LLMInputTooLargeError(estimated, self._max_input_tokens)
            logger.warning(
                "llm.input.too_large",
                extra={"input_tokens": estimated, "max_input_tokens": self._max_input_tokens},
            )
            raise too_large
        if len(parts) == 1:
            result = await self._first_pass(parts[0], rules)
        else:
            result = await self._first_pass_by_file(request, parts, rules)
        if self._verifier is None:
            return result
        with sentry_sdk.start_span(op="llm.verify", name="llm.verify") as span:
            span.set_data("findings_in", len(result.findings))
            try:
                report = await self._verifier.verify(request, rules, result.findings)
            except Exception as exc:
                # The verifier must never fail the review: publish the first pass as it is.
                logger.warning("llm.verifier.failed", extra={"error_type": type(exc).__name__})
                span.set_data("outcome", str(VerificationOutcome.FAILED))
                return dataclasses.replace(result, verification=VerificationOutcome.FAILED)
            span.set_data("findings_rejected", report.rejected)
            span.set_data("outcome", str(report.outcome))
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
        with sentry_sdk.start_span(op="llm.rule_screen", name="llm.rule_screen") as span:
            span.set_data("rules_in", len(rules))
            try:
                report = await self._rule_screen.screen(rules)
            except Exception as exc:
                # The screen must never fail the review: fall back to the pattern filter alone.
                logger.warning(
                    "llm.rule_screen.failed",
                    extra={"error_type": type(exc).__name__, "rules": len(rules)},
                )
                report = RuleScreenReport(VerificationOutcome.FAILED, unchanged)
            span.set_data("rules_dropped", report.dropped)
            span.set_data("outcome", str(report.outcome))
            return report

    def _split(self, request: ReviewRequest) -> tuple[ReviewRequest, ...]:
        if self._review_scope is ReviewScope.PR:
            return (request,)
        return tuple(
            dataclasses.replace(request, diff_text=file_diff)
            for file_diff in split_by_file(request.diff_text)
        )

    async def _first_pass_by_file(
        self, request: ReviewRequest, parts: Sequence[ReviewRequest], rules: Sequence[Rule]
    ) -> ReviewResult:
        """One first-pass call per file, then the summary agent over the per-file results.

        A file whose call fails is left out, not the review; a failed summary call falls back
        to the merged per-file summary, risk and checklist.
        """
        limit = asyncio.Semaphore(FILE_CALLS_CONCURRENCY)

        async def one(part: ReviewRequest) -> ReviewResult:
            async with limit:
                return await self._first_pass(part, rules)

        outcomes = await asyncio.gather(*(one(part) for part in parts), return_exceptions=True)
        results = [outcome for outcome in outcomes if isinstance(outcome, ReviewResult)]
        failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
        for failure in failures:
            if not isinstance(failure, LLMGatewayError):
                raise failure
        if not results:
            raise failures[0]
        if failures:
            # Their files have no findings.
            logger.warning(
                "llm.file_calls.failed", extra={"failed": len(failures), "files": len(parts)}
            )
        merged = merge_file_results(results)
        reviews = [
            file_review(part, outcome)
            for part, outcome in zip(parts, outcomes, strict=True)
            if isinstance(outcome, ReviewResult)
        ]
        summary = await self._summarize(request, reviews)
        if summary is None:
            return merged
        return dataclasses.replace(
            merged, summary=summary.summary, risk=summary.risk, checklist=summary.checklist
        )

    async def _summarize(
        self, request: ReviewRequest, reviews: Sequence[FileReview]
    ) -> ParsedSummary | None:
        """The executive summary of the whole pull request; None when no provider gives one."""
        messages = build_summary_messages(request, reviews)
        estimated = estimate_tokens(messages)
        if estimated > self._max_input_tokens:
            logger.warning(
                "llm.summary.skipped",
                extra={"input_tokens": estimated, "max_input_tokens": self._max_input_tokens},
            )
            return None
        for provider in self._providers:
            started = time.monotonic()
            try:
                parsed = parse_summary(await provider.complete(messages, purpose="summary"))
            except (LLMGatewayError, InvalidReviewJSONError) as exc:
                logger.warning(
                    "llm.summary.failed",
                    extra={
                        "provider": provider.name,
                        "model": provider.model,
                        "error_type": type(exc).__name__,
                    },
                )
                continue
            logger.info(
                "llm.summary.finished",
                extra={
                    "provider": provider.name,
                    "model": provider.model,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "files": len(reviews),
                    "risk": parsed.risk,
                    "checklist": len(parsed.checklist),
                    "risk_invalid": parsed.risk_invalid,
                    "checklist_invalid": parsed.checklist_invalid_items,
                    "checklist_trimmed": parsed.checklist_trimmed,
                },
            )
            return parsed
        # The merged file summaries are published instead.
        logger.warning("llm.summary.unavailable")
        return None

    async def _first_pass(self, request: ReviewRequest, rules: Sequence[Rule]) -> ReviewResult:
        causes: list[LLMGatewayError] = []
        for index, provider in enumerate(self._providers):
            try:
                return await review_with_provider(
                    provider, request, rules, prompt_version=self._prompt_version
                )
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


def file_review(part: ReviewRequest, result: ReviewResult) -> FileReview:
    """What the summary agent learns about one file: path, size of the change, its summary."""
    try:
        files = parse_diff(part.diff_text)
    except DiffFormatError:
        files = ()
    lines = [line for file in files for hunk in file.hunks for line in hunk.lines]
    return FileReview(
        path=(files[0].new_path or files[0].old_path) if files else "(unparsed diff)",
        added=sum(line.kind is DiffLineKind.ADDED for line in lines),
        deleted=sum(line.kind is DiffLineKind.DELETED for line in lines),
        summary=result.summary,
    )


def merge_file_results(results: Sequence[ReviewResult]) -> ReviewResult:
    """Findings of every file in order, the highest risk, the first checklist items."""
    first = results[0]
    risks = [result.risk for result in results if result.risk is not None]
    checklist = dict.fromkeys(item for result in results for item in result.checklist)
    return dataclasses.replace(
        first,
        summary=" ".join(result.summary for result in results if result.summary),
        findings=tuple(finding for result in results for finding in result.findings),
        risk=max(risks, key=RISK_ORDER.index) if risks else None,
        checklist=tuple(checklist)[:CHECKLIST_MAX_ITEMS],
    )


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
