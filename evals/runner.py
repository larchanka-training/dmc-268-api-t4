"""Run cases through the live LLM gateway. The only module in evals/ that reaches a model.

The model reply is held in memory just long enough to count the findings the parser dropped;
it is never written anywhere (AGENTS.md hard rule 3).
"""

from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from adapters.llm.config import LLMSettings
from adapters.llm.factory import build_providers
from adapters.llm.gateway import ChatProvider, FallbackLLMGateway
from adapters.llm.prompt import ChatMessage
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


class _Recorder:
    def __init__(self) -> None:
        self.last_reply: str | None = None


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

    async def complete(self, messages: Sequence[ChatMessage]) -> str:
        reply = await self._inner.complete(messages)
        self._recorder.last_reply = reply
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
        providers = [
            _RecordingProvider(provider, self._recorder)
            for provider in build_providers(settings, transport=transport)
        ]
        self._gateway = FallbackLLMGateway(providers, max_input_tokens=settings.max_input_tokens)

    async def run(self, case: Case) -> RunRecord:
        diff_text = case.read_diff()
        changed = changed_lines(diff_text)
        request = ReviewRequest(
            diff_text=diff_text, pr_title=case.title, pr_description=case.description
        )
        self._recorder.last_reply = None
        try:
            result = await self._gateway.review(request)
        except LLMGatewayError as exc:
            status = classify_failure(exc)
            outcome = ReviewOutcome(status=status)
            return RunRecord(
                case=case,
                score=score_case(case, changed, outcome),
                error=type(exc).__name__,
            )
        dropped = self._dropped()
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
        )

    def _dropped(self) -> int:
        reply = self._recorder.last_reply
        self._recorder.last_reply = None
        if reply is None:
            return 0
        try:
            return parse_review(reply).dropped_findings
        except InvalidReviewJSONError:
            return 0
