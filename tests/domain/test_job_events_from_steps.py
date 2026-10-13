"""domain.jobs.events_from_steps: the event history rebuilt from step rows."""

from datetime import UTC, datetime, timedelta

from domain.jobs import JobStep, StepRecord, StepStatus, events_from_steps, latest_steps
from domain.ports import ReviewJobStatus

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def row(
    step: JobStep,
    start: int,
    end: int | None = None,
    status: StepStatus = StepStatus.OK,
    attempt: int = 0,
    error_kind: str | None = None,
) -> StepRecord:
    return StepRecord(
        step=step,
        attempt=attempt,
        status=StepStatus.RUNNING if end is None else status,
        started_at=at(start),
        finished_at=None if end is None else at(end),
        error_kind=error_kind,
    )


def shape(events: tuple) -> list[tuple]:
    return [
        (e.seq, str(e.status), e.step and str(e.step), e.step_status and str(e.step_status))
        for e in events
    ]


def test_a_running_job_gives_a_start_and_a_finish_per_step() -> None:
    events = events_from_steps(
        "j",
        ReviewJobStatus.PROCESSING,
        0,
        at(0),
        [row(JobStep.CLAIMED, 0, 0), row(JobStep.FETCH_DIFF, 1)],
    )

    assert shape(events) == [
        (1, "PROCESSING", "CLAIMED", "RUNNING"),
        (2, "PROCESSING", "CLAIMED", "OK"),
        (3, "PROCESSING", "FETCH_DIFF", "RUNNING"),
    ]
    assert [(e.step, e.step_status) for e in latest_steps(events)] == [
        (JobStep.CLAIMED, StepStatus.OK),
        (JobStep.FETCH_DIFF, StepStatus.RUNNING),
    ]


def test_a_terminal_job_ends_with_its_status() -> None:
    events = events_from_steps(
        "j", ReviewJobStatus.COMPLETED, 0, at(9), [row(JobStep.FETCH_DIFF, 1, 2)]
    )

    assert shape(events)[-1] == (3, "COMPLETED", None, None)
    assert events[-1].at == at(9)


def test_a_retried_attempt_is_followed_by_retrying_and_earlier_seqs_never_move() -> None:
    first = [row(JobStep.LLM_CALL, 1, 5, StepStatus.FAILED, error_kind="provider_unavailable")]
    waiting = events_from_steps("j", ReviewJobStatus.RETRYING, 1, at(5), first)
    second = first + [row(JobStep.CLAIMED, 60, 60, attempt=1)]
    resumed = events_from_steps("j", ReviewJobStatus.PROCESSING, 1, at(60), second)

    assert shape(waiting) == [
        (1, "PROCESSING", "LLM_CALL", "RUNNING"),
        (2, "PROCESSING", "LLM_CALL", "FAILED"),
        (3, "RETRYING", None, None),
    ]
    assert resumed[: len(waiting)] == waiting
    assert waiting[1].error_kind == "provider_unavailable"
    assert [(e.step, e.attempt) for e in resumed[3:]] == [
        (JobStep.CLAIMED, 1),
        (JobStep.CLAIMED, 1),
    ]


def test_a_queued_job_has_no_events() -> None:
    assert events_from_steps("j", ReviewJobStatus.QUEUED, 0, at(0), []) == ()
