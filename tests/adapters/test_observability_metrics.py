"""adapters/observability/metrics.py and db_sampler.py: which names each process
exports, series started at 0, labels that a client cannot multiply, and queue gauges
that disappear when a sample fails.

Values are read from the registries in process: no metrics server starts and nothing
reaches the network. The registries are process-wide, so a test compares a value
before and after, or uses label values no other test touches.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from adapters.jobs.memory import MemoryJobStore
from adapters.observability import metrics
from adapters.observability.db_sampler import run_db_sampler
from domain.errors import ERROR_KINDS
from domain.ports import PeriodicTaskRun, QueueSnapshot, ReviewJobStatus
from tests.conftest import FakeIdentityProvider, FixedClock, build_test_app, log_events

SNAPSHOT = QueueSnapshot(
    jobs=(
        (ReviewJobStatus.QUEUED, 4),
        (ReviewJobStatus.RETRYING, 1),
        (ReviewJobStatus.PROCESSING, 2),
    ),
    oldest_claimable_age_seconds=42.5,
)


def sample_names(registry: CollectorRegistry) -> set[str]:
    return {sample.name for family in registry.collect() for sample in family.samples}


def value(registry: CollectorRegistry, name: str, **labels: str) -> float | None:
    return registry.get_sample_value(name, labels)


def count(registry: CollectorRegistry, name: str, **labels: str) -> float:
    return value(registry, name, **labels) or 0.0


@pytest.fixture
def queue_gauges() -> Any:
    """The sampler's gauges are process state: every test starts and ends without them."""
    metrics.QUEUE_GAUGES.snapshot = None
    metrics.QUEUE_GAUGES.periodic = None
    yield metrics.QUEUE_GAUGES
    metrics.QUEUE_GAUGES.snapshot = None
    metrics.QUEUE_GAUGES.periodic = None


# --- what each process exports ---------------------------------------------------


def test_the_api_and_the_worker_each_export_only_their_own_metrics(queue_gauges: Any) -> None:
    metrics.init_api_metrics([("/health", ["GET"])], webhook_providers=("github",))
    metrics.init_worker_metrics(ERROR_KINDS)
    queue_gauges.snapshot = SNAPSHOT

    api = sample_names(metrics.API_REGISTRY)
    worker = sample_names(metrics.WORKER_REGISTRY)

    assert {
        "http_requests_total",
        "http_request_duration_seconds_bucket",
        "webhook_deliveries_total",
        "queue_jobs",
        "queue_oldest_claimable_age_seconds",
    } <= api
    assert {
        "jobs_finished_total",
        "review_duration_seconds_bucket",
        "job_claim_latency_seconds_bucket",
        "worker_in_flight",
    } <= worker
    assert not api & worker


def test_no_created_series_is_exported() -> None:
    names = sample_names(metrics.API_REGISTRY) | sample_names(metrics.WORKER_REGISTRY)

    assert not [name for name in names if name.endswith("_created")]


# --- series started at 0 -----------------------------------------------------------


def test_every_route_status_class_and_webhook_outcome_starts_at_zero() -> None:
    metrics.init_api_metrics(
        [("/zero-test/{item_id}", ["GET", "POST"])], webhook_providers=("zero-forge",)
    )

    for method in ("GET", "POST"):
        for status_class in metrics.STATUS_CLASSES:
            assert (
                value(
                    metrics.API_REGISTRY,
                    "http_requests_total",
                    route="/zero-test/{item_id}",
                    method=method,
                    status_class=status_class,
                )
                == 0
            )
        assert (
            value(
                metrics.API_REGISTRY,
                "http_request_duration_seconds_count",
                route="/zero-test/{item_id}",
                method=method,
            )
            == 0
        )
    for outcome in metrics.WEBHOOK_OUTCOMES:
        assert (
            value(
                metrics.API_REGISTRY,
                "webhook_deliveries_total",
                provider="zero-forge",
                outcome=outcome,
            )
            == 0
        )


def test_job_series_start_at_zero_and_only_retryable_kinds_can_retry() -> None:
    metrics.init_worker_metrics({"zero_permanent": False, "zero_retryable": True})

    def finished(status: str, kind: str) -> float | None:
        return value(metrics.WORKER_REGISTRY, "jobs_finished_total", status=status, error_kind=kind)

    assert finished("FAILED", "zero_permanent") == 0
    assert finished("FAILED", "zero_retryable") == 0
    assert finished("RETRYING", "zero_retryable") == 0
    assert finished("RETRYING", "zero_permanent") is None
    for status in ("COMPLETED", "FAILED"):
        assert (
            value(metrics.WORKER_REGISTRY, "review_duration_seconds_count", status=status)
            is not None
        )


def test_the_stream_starts_without_a_latency_series() -> None:
    metrics.init_api_metrics([("/jobs/{job_id}/events", ["GET"])], webhook_providers=())

    assert (
        value(
            metrics.API_REGISTRY,
            "http_request_duration_seconds_count",
            route="/jobs/{job_id}/events",
            method="GET",
        )
        is None
    )


# --- recording ---------------------------------------------------------------------


def test_a_request_is_counted_by_route_method_and_status_class() -> None:
    labels = {"route": "/record-test/{item_id}", "method": "GET"}
    before = count(metrics.API_REGISTRY, "http_requests_total", **labels, status_class="4xx")
    timed = count(metrics.API_REGISTRY, "http_request_duration_seconds_count", **labels)

    metrics.observe_http("/record-test/{item_id}", "GET", 404, 0.2)

    after = count(metrics.API_REGISTRY, "http_requests_total", **labels, status_class="4xx")
    assert after == before + 1
    assert count(metrics.API_REGISTRY, "http_request_duration_seconds_count", **labels) == timed + 1


def test_a_method_outside_the_standard_set_is_counted_as_other() -> None:
    metrics.observe_http("/record-test/{item_id}", "BREW", 405, 0.01)

    labels = {"route": "/record-test/{item_id}", "status_class": "4xx"}
    assert count(metrics.API_REGISTRY, "http_requests_total", **labels, method="OTHER") >= 1
    assert value(metrics.API_REGISTRY, "http_requests_total", **labels, method="BREW") is None


def test_the_stream_is_counted_but_never_timed() -> None:
    labels = {"route": "/jobs/{job_id}/events", "method": "GET"}
    before = count(metrics.API_REGISTRY, "http_requests_total", **labels, status_class="2xx")

    metrics.observe_http("/jobs/{job_id}/events", "GET", 200, 300.0)

    after = count(metrics.API_REGISTRY, "http_requests_total", **labels, status_class="2xx")
    assert after == before + 1
    assert value(metrics.API_REGISTRY, "http_request_duration_seconds_count", **labels) is None


def test_a_terminal_job_observes_its_review_duration_and_a_retry_does_not() -> None:
    registry = metrics.WORKER_REGISTRY
    done = count(registry, "jobs_finished_total", status="COMPLETED", error_kind="")
    under_two_minutes = count(
        registry, "review_duration_seconds_bucket", status="COMPLETED", le="120.0"
    )
    under_one_minute = count(
        registry, "review_duration_seconds_bucket", status="COMPLETED", le="60.0"
    )
    retried = count(
        registry, "jobs_finished_total", status="RETRYING", error_kind="forge_unavailable"
    )
    durations = {
        status: count(registry, "review_duration_seconds_count", status=status)
        for status in ("COMPLETED", "FAILED")
    }

    metrics.record_job(ReviewJobStatus.COMPLETED, None, review_seconds=90.0)
    metrics.record_job(ReviewJobStatus.RETRYING, "forge_unavailable", review_seconds=None)

    assert count(registry, "jobs_finished_total", status="COMPLETED", error_kind="") == done + 1
    assert (
        count(registry, "review_duration_seconds_bucket", status="COMPLETED", le="120.0")
        == under_two_minutes + 1
    )
    assert (
        count(registry, "review_duration_seconds_bucket", status="COMPLETED", le="60.0")
        == under_one_minute
    )
    assert (
        count(registry, "jobs_finished_total", status="RETRYING", error_kind="forge_unavailable")
        == retried + 1
    )
    assert count(registry, "review_duration_seconds_count", status="COMPLETED") == (
        durations["COMPLETED"] + 1
    )
    assert count(registry, "review_duration_seconds_count", status="FAILED") == durations["FAILED"]


# --- the DB sampler ----------------------------------------------------------------


class ScriptedSource:
    """Answers each sample from a script (a snapshot or an exception) and records what
    the gauges held when it was asked; sets ``stop`` with its last answer."""

    def __init__(self, stop: asyncio.Event, *script: QueueSnapshot | Exception) -> None:
        self.stop = stop
        self.script = list(script)
        self.gauges_before: list[QueueSnapshot | None] = []

    async def queue_snapshot(self) -> QueueSnapshot:
        self.gauges_before.append(metrics.QUEUE_GAUGES.snapshot)
        step = self.script.pop(0)
        if not self.script:
            self.stop.set()
        if isinstance(step, Exception):
            raise step
        return step

    async def periodic_runs(self) -> tuple[PeriodicTaskRun, ...]:
        return ()


async def test_a_failed_sample_removes_the_queue_gauges(queue_gauges: Any) -> None:
    stop = asyncio.Event()
    source = ScriptedSource(stop, SNAPSHOT, OSError("connection refused"), SNAPSHOT)

    await run_db_sampler(source, stop, interval=0)

    # Absent before the first sample, set by it, removed by the failure.
    assert source.gauges_before == [None, SNAPSHOT, None]


async def test_the_gauges_carry_the_last_sample(queue_gauges: Any) -> None:
    queue_gauges.snapshot = SNAPSHOT

    registry = metrics.API_REGISTRY
    assert value(registry, "queue_jobs", status="QUEUED") == 4
    assert value(registry, "queue_jobs", status="RETRYING") == 1
    assert value(registry, "queue_jobs", status="PROCESSING") == 2
    assert value(registry, "queue_oldest_claimable_age_seconds") == 42.5


async def test_a_stopped_sampler_leaves_no_gauges(queue_gauges: Any) -> None:
    stop = asyncio.Event()

    await run_db_sampler(ScriptedSource(stop, SNAPSHOT), stop, interval=0)

    assert queue_gauges.snapshot is None
    assert value(metrics.API_REGISTRY, "queue_oldest_claimable_age_seconds") is None


async def test_failures_are_logged_once_when_they_start_and_once_when_they_clear(
    queue_gauges: Any, caplog: pytest.LogCaptureFixture
) -> None:
    stop = asyncio.Event()
    source = ScriptedSource(stop, OSError("down SECRET-HOST"), OSError("down"), SNAPSHOT, SNAPSHOT)

    with caplog.at_level(logging.INFO):
        await run_db_sampler(source, stop, interval=0)

    [failed] = log_events(caplog, "metrics.sampler.failed")
    assert failed.error_type == "OSError"
    assert len(log_events(caplog, "metrics.sampler.recovered")) == 1
    assert "SECRET-HOST" not in caplog.text


# --- the API lifespan --------------------------------------------------------------


def test_the_lifespan_serves_the_api_registry_and_stops_it(
    auth_clock: FixedClock, monkeypatch: pytest.MonkeyPatch, queue_gauges: Any
) -> None:
    served: list[tuple[CollectorRegistry, int]] = []
    stopped: list[bool] = []

    def fake_serve(registry: CollectorRegistry, port: int) -> Any:
        served.append((registry, port))
        return lambda: stopped.append(True)

    monkeypatch.setattr(metrics, "serve", fake_serve)
    app = build_test_app(
        auth_clock,
        FakeIdentityProvider(),
        metrics_port=metrics.API_PORT,
        queue_stats=MemoryJobStore(clock=auth_clock),
    )

    with TestClient(app) as client:
        client.get("/health")

    assert served == [(metrics.API_REGISTRY, metrics.API_PORT)]
    assert stopped == [True]
    assert queue_gauges.snapshot is None


# --- the metrics server ------------------------------------------------------------


class FakeServer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def shutdown(self) -> None:
        self.calls.append("shutdown")

    def server_close(self) -> None:
        self.calls.append("close")


class FakeThread:
    def join(self, timeout: float | None = None) -> None:
        self.joined = timeout


def test_serve_starts_the_server_and_its_stop_function_closes_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    server, thread = FakeServer(), FakeThread()
    started: list[tuple[int, Any]] = []

    def fake_start(port: int, registry: Any) -> tuple[FakeServer, FakeThread]:
        started.append((port, registry))
        return server, thread

    monkeypatch.setattr(metrics.prometheus_client, "start_http_server", fake_start)

    with caplog.at_level(logging.INFO):
        stop = metrics.serve(metrics.WORKER_REGISTRY, metrics.WORKER_PORT)
    stop()

    assert started == [(metrics.WORKER_PORT, metrics.WORKER_REGISTRY)]
    assert server.calls == ["shutdown", "close"]
    [record] = log_events(caplog, "metrics.serving")
    assert record.port == metrics.WORKER_PORT


def test_a_port_in_use_is_logged_and_the_process_goes_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def in_use(port: int, registry: Any) -> Any:
        raise OSError(48, "Address already in use")

    monkeypatch.setattr(metrics.prometheus_client, "start_http_server", in_use)

    stop = metrics.serve(metrics.API_REGISTRY, metrics.API_PORT)
    stop()

    [record] = log_events(caplog, "metrics.unavailable")
    assert (record.port, record.error_type) == (metrics.API_PORT, "OSError")


# --- periodic tasks (the DB sampler, phase 5) ---------------------------------------

REAPER_RUN = PeriodicTaskRun("reaper", datetime(2026, 10, 30, 12, 0, tzinfo=UTC), 30.0)


def test_the_periodic_gauges_come_from_the_last_sample(queue_gauges: Any) -> None:
    queue_gauges.periodic = (REAPER_RUN,)

    registry = metrics.API_REGISTRY
    assert (
        registry.get_sample_value("periodic_task_last_success_timestamp", {"task": "reaper"})
        == REAPER_RUN.last_success_at.timestamp()
    )
    assert registry.get_sample_value("periodic_task_interval_seconds", {"task": "reaper"}) == 30.0
    # Independent of the queue's sample.
    assert registry.get_sample_value("queue_oldest_claimable_age_seconds") is None


def test_a_task_without_a_run_has_no_series(queue_gauges: Any) -> None:
    """0 would read as "last success in 1970"; a missing series is quiet."""
    queue_gauges.periodic = ()

    assert (
        metrics.API_REGISTRY.get_sample_value(
            "periodic_task_last_success_timestamp", {"task": "reaper"}
        )
        is None
    )


class PeriodicFailing(ScriptedSource):
    async def periodic_runs(self) -> tuple[PeriodicTaskRun, ...]:
        raise RuntimeError("relation periodic_task_runs does not exist")


async def test_a_failed_periodic_read_leaves_the_queue_gauges(
    queue_gauges: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Between a deploy and its migration the table is missing: the queue still reports."""
    stop = asyncio.Event()
    snapshot = QueueSnapshot(jobs=(), oldest_claimable_age_seconds=5.0)
    source = PeriodicFailing(stop, snapshot, snapshot)
    seen: list[tuple[object, object]] = []

    async def record() -> None:
        while not stop.is_set():
            seen.append((queue_gauges.snapshot, queue_gauges.periodic))
            await asyncio.sleep(0)

    with caplog.at_level(logging.WARNING):
        await asyncio.gather(run_db_sampler(source, stop, interval=0.001), record())

    assert (snapshot, None) in seen
    assert [r.getMessage() for r in caplog.records].count("metrics.sampler.failed") == 1
