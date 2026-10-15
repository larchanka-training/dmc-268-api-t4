"""Reflection pass: JEV (TypeSafe System One) double-checks the first pass's findings.

JEV does not generate text; it answers typed questions about one state with probabilities.
The state is the same data the reviewer saw plus the numbered candidate findings, and every
finding gets one noul question: "is this a real defect in the diff?". The pass fails open:
whatever goes wrong, the first pass's findings are published unchanged.
"""

import dataclasses
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from adapters.llm.config import (
    DEFAULT_MAX_INPUT_TOKENS,
    DEFAULT_REFLECTION_KEEP_THRESHOLD,
    DEFAULT_REFLECTION_TIMEOUT_SECONDS,
    DEFAULT_TYPESAFE_BASE_URL,
    DEFAULT_TYPESAFE_MODEL,
)
from adapters.llm.jev import JevClient, JevResponse, describe_jev_error
from adapters.llm.prompt import estimate_text_tokens, findings_block, untrusted_blocks
from adapters.llm.rules import Rule
from domain.models import Finding, VerificationOutcome
from domain.ports import ReviewRequest

logger = logging.getLogger("adapters.llm")

# Fixed wording only: the finding's own text stays in the state, never in the question.
QUESTION_TEMPLATE = (
    "Finding #{index} in the findings block points at a real defect that is visible in the "
    "diff, on a line that is part of the diff, and does not contradict the repository rules."
)


@dataclass(frozen=True, slots=True)
class VerificationReport:
    outcome: VerificationOutcome
    findings: tuple[Finding, ...]
    rejected: int = 0


def build_reflection_state(
    request: ReviewRequest, rules: Sequence[Rule], findings: Sequence[Finding]
) -> str:
    return f"{untrusted_blocks(request, rules)}\n\n{findings_block(findings)}"


def question_id(index: int) -> str:
    return f"finding_{index}"


def _questions(count: int) -> dict[str, str]:
    return {
        question_id(index): QUESTION_TEMPLATE.format(index=index) for index in range(1, count + 1)
    }


class JevVerifier:
    """One POST to /v1/systemone per review, no retries."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_TYPESAFE_BASE_URL,
        model: str = DEFAULT_TYPESAFE_MODEL,
        keep_threshold: float = DEFAULT_REFLECTION_KEEP_THRESHOLD,
        timeout_seconds: float = DEFAULT_REFLECTION_TIMEOUT_SECONDS,
        max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = JevClient(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )
        self._keep_threshold = keep_threshold
        self._max_input_tokens = max_input_tokens

    async def verify(
        self, request: ReviewRequest, rules: Sequence[Rule], findings: Sequence[Finding]
    ) -> VerificationReport:
        unchanged = tuple(findings)
        if not findings:
            return VerificationReport(VerificationOutcome.SKIPPED, unchanged)
        state = build_reflection_state(request, rules, findings)
        estimated = estimate_text_tokens(state)
        if estimated > self._max_input_tokens:
            logger.warning(
                "verifier skipped: state needs ~%d tokens, over the %d-token input budget",
                estimated,
                self._max_input_tokens,
            )
            return VerificationReport(VerificationOutcome.SKIPPED, unchanged)

        started = time.monotonic()
        try:
            response = await self._client.ask_noul(state, _questions(len(findings)))
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "verifier failed (%s), keeping all %d finding(s)",
                describe_jev_error(exc),
                len(findings),
            )
            return VerificationReport(VerificationOutcome.FAILED, unchanged)
        return self._decide(response, findings, time.monotonic() - started)

    def _decide(
        self, response: JevResponse, findings: Sequence[Finding], duration: float
    ) -> VerificationReport:
        kept: list[Finding] = []
        unanswered = 0
        for index, finding in enumerate(findings, start=1):
            score = response.noul(question_id(index))
            if score is None:
                unanswered += 1
                kept.append(finding)
            elif score >= self._keep_threshold:
                kept.append(dataclasses.replace(finding, verifier_score=score))
        rejected = len(findings) - len(kept)
        usage = response.usage or {}
        logger.info(
            "verifier model=%s duration=%.2fs kept=%d dropped=%d unanswered=%d "
            "input_tokens=%s output_tokens=%s",
            response.model,
            duration,
            len(kept),
            rejected,
            unanswered,
            usage.get("input_tokens", "n/a"),
            usage.get("output_tokens", "n/a"),
        )
        return VerificationReport(VerificationOutcome.PASSED, tuple(kept), rejected)
