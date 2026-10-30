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

The metrics later phases record (the LLM cost, the canary) are added with their
instrumentation: a counter exported at 0 before anything increments it would
read as "nothing happened" rather than "not measured".
"""

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping

import prometheus_client
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily, Metric

from domain.alerts import DropReason
from domain.ports import PeriodicTaskRun, QueueSnapshot, ReviewJobStatus

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


#: The refusal paths the auth tests treat as attacks or probes (docs/observability.md §
#: Metrics): a forged OAuth state, a cross-site request, a session cookie we do not
#: know, and a 404 for an id in the path (enumeration).
SECURITY_KINDS = ("oauth_state_mismatch", "csrf_rejected", "session_rejected", "id_not_found")
SECURITY_EVENTS = Counter(
    "security_events_total",
    "Requests refused on a security path, by kind.",
    ["kind"],
    registry=API_REGISTRY,
)


class QueueGauges:
    """The DB sampler's gauges from its last successful sample, and nothing at all after
    a failed one: ``queue_jobs`` and ``queue_oldest_claimable_age_seconds`` from
    ``snapshot``, the periodic tasks' last success and interval from ``periodic`` (read
    separately, so one failing leaves the other)."""

    def __init__(self) -> None:
        self.snapshot: QueueSnapshot | None = None
        self.periodic: tuple[PeriodicTaskRun, ...] | None = None

    def collect(self) -> Iterator[Metric]:
        yield from self._periodic_gauges()
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

    def _periodic_gauges(self) -> Iterator[Metric]:
        # A timestamp gauge is absent until there is a real value: 0 would read as "last
        # success in 1970" (§ Metrics), so a task with no row yet has no series.
        runs = self.periodic
        if not runs:
            return
        last = GaugeMetricFamily(
            "periodic_task_last_success_timestamp",
            "When each periodic task last succeeded, epoch seconds (DB sampler).",
            labels=["task"],
        )
        interval = GaugeMetricFamily(
            "periodic_task_interval_seconds",
            "Each periodic task's configured interval (DB sampler).",
            labels=["task"],
        )
        for run in runs:
            last.add_metric([run.task], run.last_success_at.timestamp())
            interval.add_metric([run.task], run.interval_seconds)
        yield last
        yield interval


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
#: Step outcomes as the pr_review_steps rows record them.
STEP_OUTCOMES = ("OK", "FAILED")
LEASE_EXPIRIES = Counter(
    "lease_expiries_total",
    "Leases the reaper took back: a worker died, or a review outlived its lease.",
    registry=WORKER_REGISTRY,
)
REVIEW_STEP_DURATION = Histogram(
    "review_step_duration_seconds",
    "Duration of each recorded pipeline step, by step and outcome (OK or FAILED).",
    ["step", "outcome"],
    # A claim takes milliseconds, an LLM call minutes.
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600),
    registry=WORKER_REGISTRY,
)


class RateLimitGauge:
    """``forge_rate_limit_remaining_ratio``: per provider, the lowest
    remaining / limit seen in the last ``window`` seconds, across installations (a GitHub
    rate limit is per installation token). Absent without a sample in the window, so an
    idle process says nothing rather than "plenty left". Fed from the event loop, read
    by the metrics thread, hence the lock."""

    def __init__(self, window: float = 300.0, clock: Callable[[], float] = time.monotonic):
        self._window = window
        self._clock = clock
        self._samples: dict[str, deque[tuple[float, float]]] = {}
        self._lock = threading.Lock()

    def observe(self, provider: str, remaining: int, limit: int) -> None:
        with self._lock:
            self._samples.setdefault(provider, deque()).append((self._clock(), remaining / limit))

    def lowest(self) -> dict[str, float]:
        cutoff = self._clock() - self._window
        with self._lock:
            for samples in self._samples.values():
                while samples and samples[0][0] < cutoff:
                    samples.popleft()
            return {p: min(r for _, r in s) for p, s in self._samples.items() if s}

    def collect(self) -> Iterator[Metric]:
        lowest = self.lowest()
        if not lowest:
            return
        gauge = GaugeMetricFamily(
            "forge_rate_limit_remaining_ratio",
            "Lowest X-RateLimit-Remaining / X-RateLimit-Limit seen in the last 5 min.",
            labels=["provider"],
        )
        for provider, ratio in sorted(lowest.items()):
            gauge.add_metric([provider], ratio)
        yield gauge


# Both processes call the forge: the API lists repositories, the worker fetches diffs
# and posts reviews. Each process serves its own registry, so the gauge is in both.
FORGE_RATE_LIMIT = RateLimitGauge()
API_REGISTRY.register(FORGE_RATE_LIMIT)
WORKER_REGISTRY.register(FORGE_RATE_LIMIT)

# The LLM gateway (adapters/llm/), in the worker. One request is one HTTP call: a key
# rotation retries with the next key, so it counts twice.
LLM_PURPOSES = ("review", "correction", "summary", "rule_screen", "verify")
LLM_OUTCOMES = ("ok", "rate_limited", "key_rejected", "error")
TOKEN_DIRECTIONS = ("input", "output")
LLM_REQUESTS = Counter(
    "llm_requests_total",
    "LLM requests by provider, model, purpose and outcome; one per HTTP request.",
    ["provider", "model", "purpose", "outcome"],
    registry=WORKER_REGISTRY,
)
LLM_TOKENS = Counter(
    "llm_tokens_total",
    "LLM tokens as each provider reported them, by direction (input or output).",
    ["provider", "model", "purpose", "direction"],
    registry=WORKER_REGISTRY,
)
LLM_KEY_ROTATIONS = Counter(
    "llm_key_rotations_total",
    "API keys set aside: cooled down after a 429, or disabled after a 401 or 403.",
    ["provider"],
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
    for kind in SECURITY_KINDS:
        SECURITY_EVENTS.labels(kind)


def init_worker_metrics(error_kinds: Mapping[str, bool], *, steps: Iterable[str] = ()) -> None:
    """Start the job series at 0: COMPLETED, FAILED with every error kind, RETRYING with
    every retryable one (``error_kinds`` maps each kind to whether it is retried), and
    each step the worker records by outcome."""
    for step in steps:
        for outcome in STEP_OUTCOMES:
            REVIEW_STEP_DURATION.labels(step, outcome)
    JOBS_FINISHED.labels(ReviewJobStatus.COMPLETED.value, "")
    for kind, retryable in error_kinds.items():
        JOBS_FINISHED.labels(ReviewJobStatus.FAILED.value, kind)
        if retryable:
            JOBS_FINISHED.labels(ReviewJobStatus.RETRYING.value, kind)
    for status in (ReviewJobStatus.COMPLETED, ReviewJobStatus.FAILED):
        REVIEW_DURATION.labels(status.value)


def init_llm_metrics(
    callers: Iterable[tuple[str, str, Iterable[str], Iterable[str]]],
) -> None:
    """Start the LLM series at 0 for each (provider, model, purposes, outcomes) the
    gateway can call; adapters/llm/factory.py knows them from the configuration."""
    for provider, model, purposes, outcomes in callers:
        LLM_KEY_ROTATIONS.labels(provider)
        outcomes = tuple(outcomes)
        for purpose in purposes:
            for outcome in outcomes:
                LLM_REQUESTS.labels(provider, model, purpose, outcome)
            for direction in TOKEN_DIRECTIONS:
                LLM_TOKENS.labels(provider, model, purpose, direction)


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


def count_security_event(kind: str) -> None:
    SECURITY_EVENTS.labels(kind).inc()


def count_relay_drop(reason: DropReason) -> None:
    ALERT_RELAY_DROPPED.labels(reason.value).inc()


def count_relay_failure() -> None:
    ALERT_RELAY_FAILURES.inc()


def count_llm_request(provider: str, model: str, purpose: str, outcome: str) -> None:
    LLM_REQUESTS.labels(provider, model, purpose, outcome).inc()


def count_llm_tokens(
    provider: str, model: str, purpose: str, *, input_tokens: object, output_tokens: object
) -> None:
    """The provider's own counts; anything not a non-negative int is skipped."""
    for direction, tokens in (("input", input_tokens), ("output", output_tokens)):
        if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
            LLM_TOKENS.labels(provider, model, purpose, direction).inc(tokens)


def observe_rate_limit(provider: str, remaining: int, limit: int) -> None:
    FORGE_RATE_LIMIT.observe(provider, remaining, limit)


def count_key_rotation(provider: str) -> None:
    LLM_KEY_ROTATIONS.labels(provider).inc()


def count_lease_expiries(reaped: int) -> None:
    LEASE_EXPIRIES.inc(reaped)


def observe_step(step: str, outcome: str, seconds: float) -> None:
    REVIEW_STEP_DURATION.labels(step, outcome).observe(seconds)


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
