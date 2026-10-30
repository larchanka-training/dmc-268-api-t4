"""adapters/observability/observer.py and the review's trace: one step, four signals
(the row, the span, the duration metric, the log line), and one trace from the webhook
to the review (docs/observability.md § Review pipeline)."""

import io
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
import sentry_sdk

from adapters.jobs.memory import MemoryJobStore
from adapters.observability import metrics
from adapters.observability.observer import StepObserver
from adapters.observability.sentry import JOB_TRANSACTION, current_trace_context, job_transaction
from domain.errors import ForgeUnavailableError
from domain.jobs import JobStep, StepStatus
from domain.ports import ClaimedJob
from tests.conftest import CaptureTransport, json_lines
from tests.worker.test_process import (
    GOOD_DIFF,
    FakeGitProvider,
    claim,
    enqueue,
    make_context,
    make_payload,
)
from tests.worker.test_review_stage import FakeGateway, FakePublisher
from worker.process import ReviewStage, process_claimed_job

LOG = logging.getLogger("tests.observer")


@dataclass
class RecordingSteps:
    calls: list[tuple[str, ...]] = field(default_factory=list)
    broken: bool = False

    async def start(self, job_id: str, attempt: int, step: JobStep, *, worker_id: str) -> None:
        if self.broken:
            raise RuntimeError("database gone")
        self.calls.append(("start", step.value))

    async def finish(
        self,
        job_id: str,
        attempt: int,
        step: JobStep,
        status: StepStatus,
        *,
        worker_id: str,
        error_kind: str | None = None,
    ) -> None:
        if self.broken:
            raise RuntimeError("database gone")
        self.calls.append(("finish", step.value, status.value, str(error_kind)))


def claimed_job() -> ClaimedJob:
    return ClaimedJob(job_id="job-1", payload=make_payload(), attempt=0, worker_id="w-1")


def step_count(step: str, outcome: str) -> float:
    sample = metrics.WORKER_REGISTRY.get_sample_value(
        "review_step_duration_seconds_count", {"step": step, "outcome": outcome}
    )
    return sample or 0.0


def finished_lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [line for line in json_lines(stream) if line["event"] == "step.finished"]


async def test_a_step_writes_its_row_its_metric_and_its_log_line(json_logs: io.StringIO) -> None:
    recorder = RecordingSteps()
    before = step_count("FETCH_DIFF", "OK")

    async with StepObserver(recorder, claimed_job(), LOG).step(JobStep.FETCH_DIFF) as step:
        step.set(files=3, bytes=1200)

    assert recorder.calls == [("start", "FETCH_DIFF"), ("finish", "FETCH_DIFF", "OK", "None")]
    assert step_count("FETCH_DIFF", "OK") == before + 1
    [line] = finished_lines(json_logs)
    assert (line["step"], line["outcome"], line["job_id"]) == ("FETCH_DIFF", "OK", "job-1")
    assert isinstance(line["duration_ms"], int)
    # The attributes go on the span only; the log line keeps its fixed fields.
    assert "files" not in line


async def test_an_exception_ends_the_step_failed_with_its_error_kind_and_goes_on(
    json_logs: io.StringIO,
) -> None:
    recorder = RecordingSteps()
    before = step_count("FETCH_DIFF", "FAILED")

    with pytest.raises(ForgeUnavailableError):
        async with StepObserver(recorder, claimed_job(), LOG).step(JobStep.FETCH_DIFF):
            raise ForgeUnavailableError("github", "down")

    assert recorder.calls[-1] == ("finish", "FETCH_DIFF", "FAILED", "forge_unavailable")
    assert step_count("FETCH_DIFF", "FAILED") == before + 1
    [line] = finished_lines(json_logs)
    assert (line["outcome"], line["error_kind"]) == ("FAILED", "forge_unavailable")


async def test_a_failed_row_write_is_logged_and_the_step_still_runs(
    json_logs: io.StringIO,
) -> None:
    ran = False

    async with StepObserver(RecordingSteps(broken=True), claimed_job(), LOG).step(JobStep.CLAIMED):
        ran = True

    assert ran
    failures = [line for line in json_lines(json_logs) if line["event"] == "job.step_record.failed"]
    assert [(f["step"], f["error_type"]) for f in failures] == [
        ("CLAIMED", "RuntimeError"),
        ("CLAIMED", "RuntimeError"),
    ]
    assert finished_lines(json_logs)  # the metric and the log line still happen


async def test_without_a_recorder_only_the_rows_are_missing(json_logs: io.StringIO) -> None:
    before = step_count("CLAIMED", "OK")

    async with StepObserver(None, claimed_job(), LOG).step(JobStep.CLAIMED):
        pass

    assert step_count("CLAIMED", "OK") == before + 1
    assert finished_lines(json_logs)


async def test_each_step_is_a_span_of_the_review_job_with_its_attributes(
    sentry_capture: CaptureTransport,
) -> None:
    with job_transaction(None):
        async with StepObserver(None, claimed_job(), LOG).step(JobStep.LLM_CALL) as step:
            step.set(provider="eurouter", model="glm", findings=2)
    sentry_sdk.flush()

    [transaction] = sentry_capture.payloads("transaction")
    assert transaction["transaction"] == JOB_TRANSACTION
    assert transaction["contexts"]["trace"]["op"] == "queue.task"
    [span] = [span for span in transaction["spans"] if span["op"] == "review.step"]
    assert span["description"] == "llm_call"
    assert span["status"] == "ok"
    assert (span["data"]["provider"], span["data"]["findings"]) == ("eurouter", 2)


async def test_the_review_continues_the_trace_the_webhook_stored(
    sentry_capture: CaptureTransport,
) -> None:
    with sentry_sdk.start_transaction(name="POST /webhooks/github", op="http.server"):
        context = current_trace_context()
    assert context is not None
    trace_id = context["sentry-trace"].split("-")[0]

    with job_transaction(context):
        pass
    sentry_sdk.flush()

    [review] = [
        t for t in sentry_capture.payloads("transaction") if t["transaction"] == "review.job"
    ]
    assert review["contexts"]["trace"]["trace_id"] == trace_id


def test_with_sentry_off_no_trace_context_is_stored() -> None:
    assert current_trace_context() is None


@dataclass
class RecordingObserver:
    """The `Observer` port as a recording fake: what the worker reports per step."""

    steps: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)

    @asynccontextmanager
    async def step(self, step: JobStep, **attrs: Any):  # type: ignore[no-untyped-def]
        handle = _Handle(dict(attrs))
        try:
            yield handle
        except Exception:
            self.steps.append((step.value, handle.attrs, "FAILED"))
            raise
        self.steps.append((step.value, handle.attrs, "OK"))


class _Handle:
    def __init__(self, attrs: dict[str, Any]) -> None:
        self.attrs = attrs

    def set(self, **attrs: Any) -> None:
        self.attrs.update(attrs)


async def test_the_worker_reports_each_step_and_its_shape_never_its_content() -> None:
    store = MemoryJobStore()
    await enqueue(store, make_payload())
    observer = RecordingObserver()

    await process_claimed_job(
        await claim(store),
        FakeGitProvider(make_context(GOOD_DIFF)),
        store,
        review=ReviewStage(gateway=FakeGateway(), publisher=FakePublisher()),
        observer=observer,
    )

    assert [(name, outcome) for name, _, outcome in observer.steps] == [
        ("CLAIMED", "OK"),
        ("FETCH_DIFF", "OK"),
        ("LLM_CALL", "OK"),
        ("POST_FEEDBACK", "OK"),
    ]
    attrs = {name: attrs for name, attrs, _ in observer.steps}
    assert set(attrs["FETCH_DIFF"]) == {"bytes", "files", "reviewable"}
    assert attrs["LLM_CALL"] == {"provider": "p", "model": "m", "findings": 0}
    assert attrs["POST_FEEDBACK"] == {"comments_posted": 0, "inline_fallback": False}
    assert all(isinstance(value, int | str | bool) for a in attrs.values() for value in a.values())


async def test_the_failing_step_is_the_one_the_observer_sees_fail() -> None:
    store = MemoryJobStore()
    await enqueue(store, make_payload())
    observer = RecordingObserver()

    await process_claimed_job(
        await claim(store),
        FakeGitProvider(make_context(GOOD_DIFF)),
        store,
        review=ReviewStage(
            gateway=FakeGateway(error=ForgeUnavailableError("github", "down")),
            publisher=FakePublisher(),
        ),
        observer=observer,
    )

    assert [(name, outcome) for name, _, outcome in observer.steps] == [
        ("CLAIMED", "OK"),
        ("FETCH_DIFF", "OK"),
        ("LLM_CALL", "FAILED"),
    ]
