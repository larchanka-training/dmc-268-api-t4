"""Step metrics and the in-memory step recorder (review-context spec, [C6])."""

import pytest

from adapters.memory.job_events import InMemoryStepRecorder
from domain.jobs import JobStep, StepStatus, validate_step_metrics


async def test_step_recorder_round_trips_metrics() -> None:
    recorder = InMemoryStepRecorder()
    await recorder.start("job-1", 0, JobStep.BUILD_CONTEXT, worker_id="w1")
    await recorder.finish(
        "job-1",
        0,
        JobStep.BUILD_CONTEXT,
        StepStatus.OK,
        worker_id="w1",
        metrics={
            "context_files": 2,
            "context_fetches": 3,
            "tier1_symbols": 2,
            "tier2_symbols": 1,
            "fallback_files": 0,
            "parse_errors": 0,
            "context_tokens": 812,
            "context_cut": "none",
        },
    )
    (record,) = recorder.records("job-1")
    assert record["step"] is JobStep.BUILD_CONTEXT
    assert record["status"] is StepStatus.OK
    assert record["metrics"] is not None
    assert record["metrics"]["context_tokens"] == 812
    assert record["metrics"]["context_cut"] == "none"


async def test_step_recorder_without_metrics() -> None:
    recorder = InMemoryStepRecorder()
    await recorder.start("job-1", 0, JobStep.FETCH_DIFF, worker_id="w1")
    await recorder.finish("job-1", 0, JobStep.FETCH_DIFF, StepStatus.OK, worker_id="w1")
    (record,) = recorder.records("job-1")
    assert record["metrics"] is None


def test_metric_allowlist_rejects_unknown_keys_and_bad_values() -> None:
    with pytest.raises(ValueError):
        validate_step_metrics({"context_files": 1, "smuggled": "yes"})
    with pytest.raises(ValueError):
        validate_step_metrics({"context_files": -1})
    with pytest.raises(ValueError):
        validate_step_metrics({"context_files": True})
    with pytest.raises(ValueError):
        validate_step_metrics({"context_cut": "everywhere"})
    validate_step_metrics({"context_tokens": 5, "context_cut": "tier2"})
