"""Run cases through the live LLM gateway. The only module in evals/ that reaches a model.

Each model reply is held in memory just long enough to count the findings the parser dropped;
it is never kept or written anywhere (AGENTS.md hard rule 3).
"""

from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from adapters.llm.config import LLMSettings
from adapters.llm.factory import build_gateway
from adapters.llm.gateway import ChatProvider
from adapters.llm.prompt import ChatMessage, estimate_tokens
from adapters.llm.schema import InvalidReviewJSONError, parse_review
from domain.errors import AllProvidersFailedError, LLMGatewayError, LLMOutputError
from domain.models import Finding
from domain.ports import ReviewRequest
from evals.diffinfo import changed_lines
from evals.models import Case, PredictedFinding
from evals.scoring import CaseScore, ReviewOutcome, RunStatus, score_case


@dataclass(frozen=True, slots=True)
class RunRecord:
    case: Case
    score: CaseScore
    provider: str | None = None
    model: str | None = None
    error: str | None = None  # the exception class name only
    findings_before_verifier: int = 0
    verifier_rejected: int = 0
    # Estimated like the gateway's input budget (characters / 3), summed over every call the
    # case made, corrective retries included.
    input_tokens: int = 0
    llm_calls: int = 0
    # Published findings that carry a suggested change: a count, never the change itself.
    with_suggestion: int = 0


class _Recorder:
    """Per-case counters across every provider call: one per file in per-file mode."""

    def __init__(self) -> None:
        self.dropped = 0
        self.input_tokens = 0
        self.calls = 0

    def reset(self) -> None:
        self.dropped = 0
        self.input_tokens = 0
        self.calls = 0

    def count_dropped(self, reply: str) -> None:
        try:
            self.dropped += parse_review(reply).dropped_findings
        except InvalidReviewJSONError:
            return


class _RecordingProvider:
    def __init__(self, inner: ChatProvider, recorder: _Recorder) -> None:
        self._inner = inner
        self._recorder = recorder

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def model(self) -> str:
        return self._inner.model

    async def complete(self, messages: Sequence[ChatMessage], *, purpose: str = "review") -> str:
        self._recorder.calls += 1
        self._recorder.input_tokens += estimate_tokens(list(messages))
        reply = await self._inner.complete(messages, purpose=purpose)
        self._recorder.count_dropped(reply)
        return reply


def predicted(finding: Finding) -> PredictedFinding:
    position = finding.position
    return PredictedFinding(
        path=position.path,
        line=position.line,
        side=position.side,
        category=finding.category,
        severity=finding.severity,
    )


def classify_failure(error: LLMGatewayError) -> RunStatus:
    """A reply the model could not shape into a review is a quality miss; the rest is infra."""
    if isinstance(error, LLMOutputError):
        return RunStatus.SCHEMA_INVALID
    if (
        isinstance(error, AllProvidersFailedError)
        and error.causes
        and all(isinstance(cause, LLMOutputError) for cause in error.causes)
    ):
        return RunStatus.SCHEMA_INVALID
    return RunStatus.ERRORED


class LiveRunner:
    def __init__(
        self, settings: LLMSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._recorder = _Recorder()
        self._gateway = build_gateway(
            settings,
            transport=transport,
            wrap_provider=lambda provider: _RecordingProvider(provider, self._recorder),
        )

    async def run(self, case: Case) -> RunRecord:
        diff_text = case.read_diff()
        changed = changed_lines(diff_text)
        request = ReviewRequest(
            diff_text=diff_text, pr_title=case.title, pr_description=case.description
        )
        self._recorder.reset()
        try:
            result = await self._gateway.review(request)
        except LLMGatewayError as exc:
            status = classify_failure(exc)
            outcome = ReviewOutcome(status=status)
            return RunRecord(
                case=case,
                score=score_case(case, changed, outcome),
                error=type(exc).__name__,
                input_tokens=self._recorder.input_tokens,
                llm_calls=self._recorder.calls,
            )
        dropped = self._recorder.dropped
        outcome = ReviewOutcome(
            status=RunStatus.OK,
            findings=tuple(predicted(finding) for finding in result.findings),
            dropped=dropped,
        )
        return RunRecord(
            case=case,
            score=score_case(case, changed, outcome),
            provider=result.provider,
            model=result.model,
            findings_before_verifier=len(result.findings) + result.rejected_by_verifier,
            verifier_rejected=result.rejected_by_verifier,
            input_tokens=self._recorder.input_tokens,
            llm_calls=self._recorder.calls,
            with_suggestion=sum(finding.suggestion is not None for finding in result.findings),
        )
