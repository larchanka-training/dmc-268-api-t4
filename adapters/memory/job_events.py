"""In-memory `JobEventsSource`: events recorded in this process.

Serves tests. With PostgreSQL the API reads `adapters/db/job_events.py` instead; the
in-memory dev mode records no steps, so there every stream answers 404.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime

from domain.jobs import JobEvent, JobSnapshot, JobStep, StepStatus
from domain.ports import ReviewJobStatus


@dataclass
class _RunningStep:
    step: JobStep
    attempt: int
    started_at: datetime


class InMemoryStepRecorder:
    """Implements `domain.ports.StepRecorder` in memory: the twin of
    `PostgresStepRecorder` for tests and the in-memory dev mode.

    Keeps the latest record per (step, attempt), like the `pr_review_steps`
    table does, and returns them through `records()` for assertions.
    """

    def __init__(self) -> None:
        self._steps: dict[tuple[str, int, JobStep], dict[str, object]] = {}
        self._running: dict[tuple[str, str], _RunningStep] = {}

    async def start(self, job_id: str, attempt: int, step: JobStep, *, worker_id: str) -> None:
        self._running[(job_id, worker_id)] = _RunningStep(step, attempt, datetime.now())

    async def finish(
        self,
        job_id: str,
        attempt: int,
        step: JobStep,
        status: StepStatus,
        *,
        worker_id: str,
        error_kind: str | None = None,
        metrics: Mapping[str, int | str] | None = None,
    ) -> None:
        key = (job_id, worker_id)
        running = self._running.pop(key, None)
        started = running.started_at if running is not None else datetime.now()
        self._steps[(job_id, attempt, step)] = {
            "step": step,
            "attempt": attempt,
            "status": status,
            "started_at": started,
            "finished_at": datetime.now(),
            "error_kind": error_kind,
            "metrics": dict(metrics) if metrics else None,
        }

    def records(self, job_id: str) -> tuple[dict[str, object], ...]:
        return tuple(
            record
            for (record_job_id, _, _), record in sorted(
                self._steps.items(), key=lambda item: (item[0][1], item[1]["started_at"])
            )
            if record_job_id == job_id
        )


class InMemoryJobEventsSource:
    def __init__(self) -> None:
        self._jobs: dict[str, JobSnapshot] = {}
        self._events: dict[str, list[JobEvent]] = {}
        self._changed = asyncio.Condition()

    def create(
        self, job_id: str, installation_id: int, status: ReviewJobStatus = ReviewJobStatus.QUEUED
    ) -> None:
        self._jobs[job_id] = JobSnapshot(
            job_id=job_id, installation_id=installation_id, status=status, last_seq=0
        )
        self._events[job_id] = []

    async def record(self, event: JobEvent) -> None:
        """Append an event and wake every waiting stream."""
        async with self._changed:
            self.record_nowait(event)
            self._changed.notify_all()

    def record_nowait(self, event: JobEvent) -> None:
        """Synchronous variant for setting up state before a stream opens."""
        snapshot = self._jobs[event.job_id]
        if event.seq <= snapshot.last_seq:
            raise ValueError(f"event seq must increase: {event.seq} after {snapshot.last_seq}")
        self._events[event.job_id].append(event)
        steps = snapshot.steps
        if event.step is not None:
            # Keep one row per (step, attempt): the latest state of that step.
            steps = tuple(
                existing
                for existing in steps
                if (existing.step, existing.attempt) != (event.step, event.attempt)
            ) + (event,)
        self._jobs[event.job_id] = replace(
            snapshot, status=event.status, last_seq=event.seq, steps=steps
        )

    async def snapshot(self, job_id: str) -> JobSnapshot | None:
        return self._jobs.get(job_id)

    async def next_events(
        self, job_id: str, after_seq: int, timeout_seconds: float
    ) -> tuple[JobEvent, ...]:
        async with self._changed:
            try:
                await asyncio.wait_for(
                    self._changed.wait_for(lambda: bool(self._pending(job_id, after_seq))),
                    timeout_seconds,
                )
            except TimeoutError:
                return ()
            return self._pending(job_id, after_seq)

    def _pending(self, job_id: str, after_seq: int) -> tuple[JobEvent, ...]:
        return tuple(event for event in self._events.get(job_id, ()) if event.seq > after_seq)
