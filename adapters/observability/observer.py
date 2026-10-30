"""The worker's `Observer`: every signal of a pipeline step from one place.

docs/observability.md § Review pipeline. Per step it writes the ``pr_review_steps``
row through the `StepRecorder`, opens a Sentry span named after the step (lower-cased)
inside the job's ``review.job`` transaction, observes
``review_step_duration_seconds``, and logs ``step.finished``. An exception leaving the
block ends the step FAILED with the error_kind ``domain.errors.classify`` gives it, and
is raised on: the job's own failure handling decides what happens to the job.

The step attributes go on the span only ("shape, never content": counts, sizes, the
provider and model); the log line keeps its fixed fields, which the log allowlist
knows. Recording is progress for the console, never part of the job's outcome: a
failed row write is logged (exception class only) and the step goes on.
"""

import logging
import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager

import sentry_sdk

from adapters.observability import metrics
from domain.errors import classify
from domain.jobs import JobStep, StepStatus
from domain.ports import ClaimedJob, StepHandle, StepRecorder


class _Handle:
    def __init__(self, attrs: dict[str, str | int | bool]) -> None:
        self.attrs = attrs

    def set(self, **attrs: str | int | bool) -> None:
        self.attrs.update(attrs)


class StepObserver:
    """The `Observer` for one claimed job; without a recorder no rows are written."""

    def __init__(
        self, recorder: StepRecorder | None, claimed: ClaimedJob, logger: logging.Logger
    ) -> None:
        self._recorder = recorder
        self._claimed = claimed
        self._log = logger

    @asynccontextmanager
    async def step(self, step: JobStep, **attrs: str | int | bool) -> AsyncIterator[StepHandle]:
        handle = _Handle(dict(attrs))
        claimed = self._claimed
        if self._recorder is not None:
            await self._guarded(
                step,
                self._recorder.start(
                    claimed.job_id, claimed.attempt, step, worker_id=claimed.worker_id
                ),
            )
        started = time.monotonic()
        with sentry_sdk.start_span(op="review.step", name=step.value.lower()) as span:
            try:
                yield handle
            except Exception as error:
                kind, _ = classify(error)
                span.set_status("internal_error")
                await self._finish(step, StepStatus.FAILED, kind, started)
                raise
            else:
                span.set_status("ok")
                await self._finish(step, StepStatus.OK, None, started)
            finally:
                for key, value in handle.attrs.items():
                    span.set_data(key, value)

    async def _finish(
        self, step: JobStep, status: StepStatus, error_kind: str | None, started: float
    ) -> None:
        seconds = time.monotonic() - started
        claimed = self._claimed
        if self._recorder is not None:
            await self._guarded(
                step,
                self._recorder.finish(
                    claimed.job_id,
                    claimed.attempt,
                    step,
                    status,
                    worker_id=claimed.worker_id,
                    error_kind=error_kind,
                ),
            )
        metrics.observe_step(step.value, status.value, seconds)
        fields: dict[str, object] = {
            "job_id": claimed.job_id,
            "step": step.value,
            "outcome": status.value,
            "duration_ms": round(seconds * 1000),
        }
        if error_kind is not None:
            fields["error_kind"] = error_kind
        self._log.info("step.finished", extra=fields)

    async def _guarded(self, step: JobStep, write: Awaitable[None]) -> None:
        try:
            await write
        except Exception as err:
            self._log.error(
                "job.step_record.failed",
                extra={
                    "job_id": self._claimed.job_id,
                    "step": step.value,
                    "error_type": type(err).__name__,
                },
            )
