import asyncio
import dataclasses
import logging
import time
from collections.abc import Sequence
from typing import Protocol

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

    async def complete(self, messages: Sequence[ChatMessage]) -> str: ...


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
            "provider=%s model=%s dropped %d finding(s) that failed schema validation (%s)",
            provider.name,
            provider.model,
            parsed.dropped_findings,
            ", ".join(f"{name}={count}" for name, count in sorted(parsed.dropped_fields.items())),
        )
    if parsed.unknown_rule_ids:
        logger.warning(
            "provider=%s model=%s cleared rule_id on %d finding(s) citing no listed rule",
            provider.name,
            provider.model,
            parsed.unknown_rule_ids,
        )
    if prompt_version in PROMPTS_WITH_RISK:
        _log_summary_fields(provider, parsed)
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
            "provider=%s model=%s summary fields: risk %s, checklist %d invalid item(s), "
            "%d over the limit",
            provider.name,
            provider.model,
            risk_problem or "ok",
            parsed.checklist_invalid_items,
            parsed.checklist_trimmed,
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
            logger.warning("%s", too_large)
            raise too_large
        if len(parts) == 1:
            result = await self._first_pass(parts[0], rules)
        else:
            result = await self._first_pass_by_file(request, parts, rules)
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
            logger.warning(
                "per-file review: %d of %d file call(s) failed, their files have no findings",
                len(failures),
                len(parts),
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
                "summary agent skipped: ~%d tokens over the %d-token input budget",
                estimated,
                self._max_input_tokens,
            )
            return None
        for provider in self._providers:
            started = time.monotonic()
            try:
                parsed = parse_summary(await provider.complete(messages))
            except (LLMGatewayError, InvalidReviewJSONError) as exc:
                logger.warning(
                    "summary agent provider=%s model=%s failed (%s)",
                    provider.name,
                    provider.model,
                    type(exc).__name__,
                )
                continue
            logger.info(
                "summary agent provider=%s model=%s duration=%.2fs files=%d risk=%s "
                "checklist=%d risk_invalid=%s checklist_invalid=%d checklist_trimmed=%d",
                provider.name,
                provider.model,
                time.monotonic() - started,
                len(reviews),
                parsed.risk or "none",
                len(parsed.checklist),
                parsed.risk_invalid,
                parsed.checklist_invalid_items,
                parsed.checklist_trimmed,
            )
            return parsed
        logger.warning("summary agent failed on every provider, using the merged file summaries")
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
                        "%s; falling back to provider=%s", exc, self._providers[index + 1].name
                    )
        error = AllProvidersFailedError(causes)
        logger.error("%s", error)
        raise error


def file_review(part: ReviewRequest, result: ReviewResult) -> FileReview:
    """What the summary agent learns about one file: path, size of the change, the result."""
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
        findings=result.findings,
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
        "repository rules: kept %d, dropped %d by pattern, %d by JEV (screen %s), "
        "%d over the limits",
        len(screen.rules),
        rule_set.dropped_suspicious,
        screen.dropped,
        screen.outcome,
        rule_set.dropped_over_limit,
    )
