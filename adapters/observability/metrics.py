"""Prometheus metrics of the API and the worker (docs/observability.md § Metrics).

Every application metric is defined here, so the names are greppable. Each process
serves a registry of its own on an internal port, never on the API's main port: the
API on 9100 (started in its lifespan), the worker on 9101. Neither port is published;
Alloy scrapes them on the compose network.

Labels are low-cardinality only: never an account, installation, repository or job
id, and never a raw path. A request's route is its template, and a method outside the
standard set is counted as ``OTHER``, so a client cannot create series.

Counters and histograms start at 0 for every known label combination: an alert rule
reads a missing series as "not firing", so "No throughput" could never fire after a
restart with no traffic. The sampled queue gauges are the exception: they exist only
while the last sample succeeded, so a stale value never looks current.

The metrics later phases record (LLM calls, forge rate limit, security events, steps,
the canary) are added with their instrumentation: a counter exported at 0 before
anything increments it would read as "nothing happened" rather than "not measured".
"""

import logging
from collections.abc import Callable, Iterable, Iterator, Mapping

import prometheus_client
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily, Metric

from domain.alerts import DropReason
from domain.ports import QueueSnapshot, ReviewJobStatus

API_PORT = 9100
WORKER_PORT = 9101

# Every counter and histogram would also export a `_created` series: twice the series
# against the free tier's 10k, and no dashboard or rule reads them.
prometheus_client.disable_created_metrics()  # type: ignore[no-untyped-call]  # unannotated upstream

API_REGISTRY = CollectorRegistry()
WORKER_REGISTRY = CollectorRegistry()

STATUS_CLASSES = ("2xx", "3xx", "4xx", "5xx")
WEBHOOK_OUTCOMES = (
    "enqueued",
    "duplicate",
    "filtered",
    "unauthorized",
    "too_large",
    "rejected",
    "error",
)
#: The SSE stream stays open until its job ends: counted, never timed.
UNTIMED_ROUTES = frozenset({"/jobs/{job_id}/events"})
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})

logger = logging.getLogger(__name__)

# --- API ---------------------------------------------------------------------------

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "API requests by route template, method and status class.",
    ["route", "method", "status_class"],
    registry=API_REGISTRY,
)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "API request latency by route template and method; the SSE stream is not observed.",
    ["route", "method"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=API_REGISTRY,
)
WEBHOOK_DELIVERIES = Counter(
    "webhook_deliveries_total",
    "Webhook deliveries by provider and outcome.",
    ["provider", "outcome"],
    registry=API_REGISTRY,
)


# The Sentry → Telegram relay (api/internal_alerts.py). Alerted from Grafana, never from
# Sentry: a relay failure must not become a Sentry issue that triggers the relay again.
ALERT_RELAY_FAILURES = Counter(
    "alert_relay_failures_total",
    "Sentry alerts the relay failed to deliver to Telegram.",
    registry=API_REGISTRY,
)
ALERT_RELAY_DROPPED = Counter(
    "alert_relay_dropped_total",
    "Sentry alerts the relay chose not to forward, by reason.",
    ["reason"],
    registry=API_REGISTRY,
)


class QueueGauges:
    """``queue_jobs`` and ``queue_oldest_claimable_age_seconds`` from the DB sampler's
    last successful sample, and nothing at all after a failed one."""

    def __init__(self) -> None:
        self.snapshot: QueueSnapshot | None = None

    def collect(self) -> Iterator[Metric]:
        snapshot = self.snapshot
        if snapshot is None:
            return
        jobs = GaugeMetricFamily(
            "queue_jobs", "Jobs per queue status (DB sampler).", labels=["status"]
        )
        for status, count in snapshot.jobs:
            jobs.add_metric([status.value], count)
        yield jobs
        yield GaugeMetricFamily(
            "queue_oldest_claimable_age_seconds",
            "Age of the oldest job that could be claimed now, from its next_attempt_at.",
            value=snapshot.oldest_claimable_age_seconds,
        )


QUEUE_GAUGES = QueueGauges()
API_REGISTRY.register(QUEUE_GAUGES)

# --- worker ------------------------------------------------------------------------

JOBS_FINISHED = Counter(
    "jobs_finished_total",
    "Review attempts by the status they ended in and their error_kind (empty when completed).",
    ["status", "error_kind"],
    registry=WORKER_REGISTRY,
)
REVIEW_DURATION = Histogram(
    "review_duration_seconds",
    "Enqueue to terminal status per job: queue wait and earlier attempts included.",
    ["status"],
    # 60, 120, 300, 600 and 900 are edges, so the latency SLO threshold sits on one.
    buckets=(15, 30, 60, 120, 180, 300, 450, 600, 900, 1800, 3600),
    registry=WORKER_REGISTRY,
)
JOB_CLAIM_LATENCY = Histogram(
    "job_claim_latency_seconds",
    "From when a job became claimable (next_attempt_at) to its claim.",
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300, 900),
    registry=WORKER_REGISTRY,
)
WORKER_IN_FLIGHT = Gauge(
    "worker_in_flight",
    "Jobs this worker process is running now.",
    registry=WORKER_REGISTRY,
)


# --- initialisation ----------------------------------------------------------------


def init_api_metrics(
    routes: Iterable[tuple[str, Iterable[str]]], *, webhook_providers: Iterable[str]
) -> None:
    """Start the API's counters and histograms at 0: every route template and method
    by status class, every webhook outcome and every relay drop reason. Calling it
    again changes nothing."""
    for route, methods in routes:
        for method in methods:
            for status_class in STATUS_CLASSES:
                HTTP_REQUESTS.labels(route, method, status_class)
            if route not in UNTIMED_ROUTES:
                HTTP_REQUEST_DURATION.labels(route, method)
    for provider in webhook_providers:
        for outcome in WEBHOOK_OUTCOMES:
            WEBHOOK_DELIVERIES.labels(provider, outcome)
    for reason in DropReason:
        ALERT_RELAY_DROPPED.labels(reason.value)


def init_worker_metrics(error_kinds: Mapping[str, bool]) -> None:
    """Start the job series at 0: COMPLETED, FAILED with every error kind, RETRYING with
    every retryable one (``error_kinds`` maps each kind to whether it is retried)."""
    JOBS_FINISHED.labels(ReviewJobStatus.COMPLETED.value, "")
    for kind, retryable in error_kinds.items():
        JOBS_FINISHED.labels(ReviewJobStatus.FAILED.value, kind)
        if retryable:
            JOBS_FINISHED.labels(ReviewJobStatus.RETRYING.value, kind)
    for status in (ReviewJobStatus.COMPLETED, ReviewJobStatus.FAILED):
        REVIEW_DURATION.labels(status.value)


# --- recording ---------------------------------------------------------------------


def observe_http(route: str, method: str, status: int, seconds: float) -> None:
    """One API request; ``route`` is its template (or the access log's unmatched one)."""
    method = method if method in _METHODS else "OTHER"
    HTTP_REQUESTS.labels(route, method, f"{status // 100}xx").inc()
    if route not in UNTIMED_ROUTES:
        HTTP_REQUEST_DURATION.labels(route, method).observe(seconds)


def count_webhook(provider: str, outcome: str) -> None:
    WEBHOOK_DELIVERIES.labels(provider, outcome).inc()


def record_job(
    status: ReviewJobStatus, error_kind: str | None, *, review_seconds: float | None
) -> None:
    """One attempt's outcome. ``review_seconds`` (enqueue to now) is observed for a
    terminal status only; None when the store did not say when the job was enqueued."""
    JOBS_FINISHED.labels(status.value, error_kind or "").inc()
    if review_seconds is not None and status is not ReviewJobStatus.RETRYING:
        REVIEW_DURATION.labels(status.value).observe(review_seconds)


def count_relay_drop(reason: DropReason) -> None:
    ALERT_RELAY_DROPPED.labels(reason.value).inc()


def count_relay_failure() -> None:
    ALERT_RELAY_FAILURES.inc()


def observe_claim_latency(seconds: float) -> None:
    JOB_CLAIM_LATENCY.observe(max(seconds, 0.0))


# --- serving -----------------------------------------------------------------------


def serve(registry: CollectorRegistry, port: int) -> Callable[[], None]:
    """Serve ``registry`` on ``port`` from a daemon thread; returns the stop function.

    All interfaces inside the container, so Alloy reaches it on the compose network;
    compose publishes no metrics port (docs/observability.md § Metrics). A port that
    cannot be bound is logged and skipped, like an unwritable log file: metrics must not
    take the process down, and the scrape target being down is what reports it.
    """
    try:
        server, thread = prometheus_client.start_http_server(port, registry=registry)
    except OSError as error:
        logger.warning(
            "metrics.unavailable", extra={"port": port, "error_type": type(error).__name__}
        )
        return lambda: None
    logger.info("metrics.serving", extra={"port": port})

    def stop() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    return stop
