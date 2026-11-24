"""JEV (TypeSafe System One) client: typed questions about one state, answered with scores."""

import time
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from adapters.llm.config import (
    DEFAULT_REFLECTION_TIMEOUT_SECONDS,
    DEFAULT_TYPESAFE_BASE_URL,
    DEFAULT_TYPESAFE_MODEL,
)
from adapters.observability import metrics

#: The provider label of TypeSafe's requests (llm_requests_total, llm_tokens_total).
TYPESAFE = "typesafe"


class JevAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    noul: float | None = None


class JevResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    answers: dict[str, JevAnswer]
    usage: dict[str, Any] | None = None

    def noul(self, question: str) -> float | None:
        """The answer's score, or None when it is missing or out of [0, 1]."""
        answer = self.answers.get(question)
        score = answer.noul if answer is not None else None
        if score is None or not 0.0 <= score <= 1.0:
            return None
        return score


def describe_jev_error(exc: Exception) -> str:
    """Exception class plus the HTTP status: never the message, which may echo the request."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{type(exc).__name__}, HTTP {exc.response.status_code}"
    return type(exc).__name__


class JevClient:
    """One POST to /v1/systemone per call, no retries."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_TYPESAFE_BASE_URL,
        model: str = DEFAULT_TYPESAFE_MODEL,
        timeout_seconds: float = DEFAULT_REFLECTION_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/v1/systemone"
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def ask_noul(
        self, state: str, questions: Mapping[str, str], *, purpose: str
    ) -> JevResponse:
        """Raises httpx.HTTPError on transport or HTTP errors, ValueError on a bad reply.
        ``purpose`` (rule_screen, verify) labels the request's metrics."""
        started = time.monotonic()
        try:
            response = await self._ask(state, questions)
        except Exception:
            metrics.count_llm_request(TYPESAFE, self._model, purpose, "error")
            raise
        metrics.count_llm_request(TYPESAFE, self._model, purpose, "ok")
        metrics.observe_llm_latency(TYPESAFE, self._model, purpose, time.monotonic() - started)
        usage = response.usage or {}
        metrics.count_llm_tokens(
            TYPESAFE,
            self._model,
            purpose,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )
        return response

    async def _ask(self, state: str, questions: Mapping[str, str]) -> JevResponse:
        payload_questions = {
            question: {"type": "noul", "instructions": instructions}
            for question, instructions in questions.items()
        }
        async with httpx.AsyncClient(
            transport=self._transport, timeout=httpx.Timeout(self._timeout_seconds)
        ) as client:
            response = await client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "state": state, "questions": payload_questions},
            )
        response.raise_for_status()
        try:
            return JevResponse.model_validate(response.json())
        except ValidationError as exc:
            raise ValueError(
                f"unexpected verifier response ({exc.error_count()} error(s))"
            ) from None
