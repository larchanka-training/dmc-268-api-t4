#!/usr/bin/env python3
"""Run the observability lab, check the whole pipeline and rehearse failures.

    uv run deploy/lab/lab.py test           # up, check, down
    uv run deploy/lab/lab.py test --keep    # the same, then leave it running
    uv run deploy/lab/lab.py up | check | down
    uv run deploy/lab/lab.py send enqueued --count 5
    uv run deploy/lab/lab.py drill worker-down [--alert]
    uv run deploy/lab/lab.py sync           # reload dashboards and rules into Grafana
    uv run deploy/lab/lab.py relay [--stale]  # a signed Sentry alert to the relay

``check`` sends GitHub-style webhooks and follows them through every hop: the API and
worker metrics endpoints, Caddy, Alloy, Prometheus and Loki. Counters are compared
before and after, so it can run again on a lab that is already up. ``drill`` breaks one
part on purpose, checks the signals an alert would read, and repairs it; with
``--alert`` it holds the part broken until the Grafana alert fires. ``up`` loads the
dashboards and alert rules into the lab's Grafana with ops/observability/sync.py. The
standard library only, but for that sync (httpx and PyYAML, hence uv run). See
deploy/lab/README.md.
"""

import argparse
import hashlib
import hmac
import http.client
import json
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LAB = Path(__file__).resolve().parent
COMPOSE = ["docker", "compose", "-f", str(LAB / "compose.yml")]
PRODUCTION_COMPOSE = LAB.parent / "compose.yml"

API = "http://127.0.0.1:8000"
API_METRICS = "http://127.0.0.1:9100/metrics"
WORKER_METRICS = "http://127.0.0.1:9101/metrics"
CADDY = "https://localhost:8443"
ALLOY = "http://127.0.0.1:12345"
PROMETHEUS = "http://127.0.0.1:9090"
GRAFANA = "http://127.0.0.1:3000"
SENTRY_STUB = "http://127.0.0.1:9900"
RELAY_PATH = "/internal/alerts/sentry"
RELAY = f"{API}{RELAY_PATH}"
URLS = f"Grafana {GRAFANA}, Prometheus {PROMETHEUS}, Alloy {ALLOY}"

# The values compose.yml gives the app and Alloy.
WEBHOOK_SECRET = "local-webhook-secret"
SENTRY_WEBHOOK_SECRET = "lab-sentry-webhook-secret"
STUB_DSN = "http://lab@sentry:9000/1"
EXPORTER_PASSWORD = "exporter-lab"
MAX_BODY = 5 * 1024 * 1024

#: Every scrape target in deploy/alloy/config.alloy, by its job label.
TARGETS = frozenset({"alloy", "api", "cadvisor", "caddy", "host", "postgres", "worker"})
#: Loki index labels: ours (service, level), the file source's and the one Loki derives.
LOKI_LABELS = frozenset({"service", "level", "filename", "service_name"})
#: Production images the lab must use too.
SHARED_IMAGES = ("grafana/alloy", "caddy", "postgres")

REPO = LAB.parents[1]
#: The lab's stand-in for the telegram contact point: a webhook to a closed port, so a
#: firing alert shows in Grafana and goes nowhere. sync.py creates the contact point only
#: when it is missing, so it keeps this one.
LAB_CONTACT_POINT = {
    "name": "telegram",
    "type": "webhook",
    "settings": {"url": "http://127.0.0.1:9/the-lab-has-no-telegram"},
}
#: The lab's Prometheus data source (compose.yml, configs.datasources).
LAB_PROM_UID = "prom"

# Alloy scrapes every 60 s, so a fresh value takes up to a minute and a bit to arrive.
PIPELINE_TIMEOUT = 180
#: A drill with --alert holds the part broken until its Grafana alert fires: a scrape, an
#: evaluation, the rule's `for` (TelemetryMissing needs 5 min of silence).
ALERT_TIMEOUT = 600
#: A sample older than this is not a live target: an instant query falls back on samples
#: up to 5 min old, so a target that just broke would still look up.
FRESH_SECONDS = 120

# Idempotent, so `up` can run on a lab that is already up.
EXPORTER_ROLE_SQL = f"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dmc268_exporter') THEN
    CREATE ROLE dmc268_exporter LOGIN PASSWORD '{EXPORTER_PASSWORD}';
  END IF;
END
$$;
GRANT pg_monitor TO dmc268_exporter;
"""

# Caddy signs "localhost" with its own CA.
_UNVERIFIED = ssl.create_default_context()
_UNVERIFIED.check_hostname = False
_UNVERIFIED.verify_mode = ssl.CERT_NONE


# --- docker compose ----------------------------------------------------------------


def compose(*args: str) -> None:
    subprocess.run([*COMPOSE, *args], check=True)


def running() -> bool:
    """Whether any lab container exists, running or not."""
    listed = subprocess.run([*COMPOSE, "ps", "-aq"], capture_output=True, text=True, check=True)
    return bool(listed.stdout.strip())


def check_images() -> None:
    """Stop when the lab would test other images than production runs."""

    def images(path: Path) -> dict[str, str]:
        found = re.findall(r"^\s*image:\s*(\S+?):(\S+)\s*$", path.read_text(), re.M)
        return {name: tag for name, tag in found if name in SHARED_IMAGES}

    lab, production = images(LAB / "compose.yml"), images(PRODUCTION_COMPOSE)
    drift = [
        f"{name}: lab {lab.get(name)}, production {production.get(name)}"
        for name in SHARED_IMAGES
        if lab.get(name) != production.get(name)
    ]
    if drift:
        sys.exit("deploy/lab/compose.yml is behind deploy/compose.yml: " + "; ".join(drift))


def up() -> None:
    check_images()
    step("Building the app image")
    compose("build")
    step("Starting Postgres")
    compose("up", "-d", "--wait", "postgres")
    # The two manual steps production needs after the merge (observability checklist).
    step("Migrating (alembic upgrade head)")
    compose("run", "--rm", "--no-deps", "api", "alembic", "upgrade", "head")
    step("Creating the dmc268_exporter role")
    compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-q",
        "-U",
        "dmc",
        "-d",
        "dmc268",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        EXPORTER_ROLE_SQL,
    )
    step("Starting everything else")
    compose("up", "-d", "--wait")
    step("Loading dashboards and alert rules into Grafana (ops/observability/sync.py)")
    sync_grafana()


def sync_grafana() -> None:
    """Production's dashboards, rules, mute timings and policy, into the lab's Grafana."""
    sys.path.insert(0, str(REPO))
    from ops.observability import sync

    poll(lambda: request(f"{GRAFANA}/api/health")[0], lambda status: status == 200, 60)
    _, body = request(f"{GRAFANA}/api/v1/provisioning/contact-points?name=telegram")
    if json.loads(body) == []:
        request(
            f"{GRAFANA}/api/v1/provisioning/contact-points",
            method="POST",
            body=json.dumps(LAB_CONTACT_POINT).encode(),
            headers={"Content-Type": "application/json", "X-Disable-Provenance": "true"},
        )
    settings = sync.Settings(grafana_url=GRAFANA, prom_datasource_uid=LAB_PROM_UID)
    try:
        sync.run(settings, ("dashboards", "rules", "alerting"))
    except sync.SyncError as error:
        # check reports the missing rules or dashboards; up goes on.
        print(f"  Grafana sync failed: {error}")


def down() -> None:
    step("Removing the lab: containers, network and volumes")
    compose("down", "--volumes", "--remove-orphans")


# --- HTTP --------------------------------------------------------------------------


def request(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    context = _UNVERIFIED if url.startswith("https:") else None
    try:
        with urllib.request.urlopen(req, timeout=10, context=context) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def send_webhook(event: str, delivery: str, payload: object, *, signed: bool = True) -> int:
    """One delivery as GitHub sends it: the HMAC of the exact body bytes."""
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
    }
    if signed:
        digest = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={digest}"
    status, _ = request(f"{API}/webhooks/github", method="POST", body=body, headers=headers)
    return status


def send_oversized() -> int:
    """Declares a body over the 5 MB cap and sends none: the API refuses on the header."""
    connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=10)
    try:
        connection.putrequest("POST", "/webhooks/github")
        connection.putheader("Content-Length", str(MAX_BODY + 1))
        connection.putheader("X-GitHub-Event", "pull_request")
        connection.endheaders()
        return connection.getresponse().status
    finally:
        connection.close()


def pr_payload(title: str, number: int = 7) -> dict[str, object]:
    """A reviewable pull_request delivery (tests/api/test_webhook_github.py)."""
    return {
        "action": "opened",
        "installation": {"id": 42},
        "repository": {"id": 751065667, "full_name": "acme/web"},
        "pull_request": {
            "number": number,
            "head": {"sha": "a1b2c3d4e5" * 4, "ref": "feature"},
            "base": {"sha": "f6e5d4c3b2" * 4, "ref": "main"},
            "title": title,
            "user": {"login": "octocat", "id": 1},
            "author_association": "CONTRIBUTOR",
            "draft": False,
        },
    }


#: The answer the API gives each outcome (docs/observability.md § Application).
EXPECTED_STATUS = {
    "enqueued": 202,
    "duplicate": 202,
    "filtered": 204,
    "unauthorized": 401,
    "rejected": 400,
    "too_large": 413,
}


def deliver(outcome: str, delivery: str, *, pr: int = 7, title: str = "Lab PR") -> int:
    """One delivery the API counts as ``outcome``; returns the HTTP status. ``duplicate``
    resends ``delivery``, which must have been enqueued already."""
    if outcome in ("enqueued", "duplicate"):
        return send_webhook("pull_request", delivery, pr_payload(title, pr))
    if outcome == "filtered":
        return send_webhook("ping", delivery, {"zen": "lab"})
    if outcome == "unauthorized":
        return send_webhook("ping", delivery, {}, signed=False)
    if outcome == "rejected":
        # Signed, but with no pull request in it.
        return send_webhook("pull_request", delivery, {"action": "opened"})
    if outcome == "too_large":
        return send_oversized()
    raise ValueError(f"unknown outcome {outcome!r}")


def new_delivery() -> str:
    return f"lab-{time.time_ns()}"


# --- Prometheus text format, Prometheus and Loki APIs --------------------------------

_SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})? (\S+)$")
_LABEL = re.compile(r'([a-zA-Z_]\w*)="((?:[^"\\]|\\.)*)"')

Samples = list[tuple[str, dict[str, str], float]]


def scrape(url: str) -> Samples:
    _, body = request(url)
    samples: Samples = []
    for line in body.decode().splitlines():
        match = _SAMPLE.match(line)
        if match:
            samples.append((match[1], dict(_LABEL.findall(match[2] or "")), float(match[3])))
    return samples


def value(samples: Samples, name: str, **labels: str) -> float:
    """The sum of the series of ``name`` whose labels include ``labels``; 0 if none."""
    return sum(
        number
        for sample, sample_labels, number in samples
        if sample == name and all(sample_labels.get(k) == v for k, v in labels.items())
    )


def has_series(samples: Samples, name: str, **labels: str) -> bool:
    return any(
        sample == name and all(sample_labels.get(k) == v for k, v in labels.items())
        for sample, sample_labels, _ in samples
    )


def api_value(name: str, **labels: str) -> float:
    return value(scrape(API_METRICS), name, **labels)


def worker_value(name: str, **labels: str) -> float:
    return value(scrape(WORKER_METRICS), name, **labels)


def promql(query: str) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({"query": query})
    _, body = request(f"{PROMETHEUS}/api/v1/query?{params}")
    result: list[dict[str, Any]] = json.loads(body)["data"]["result"]
    return result


def fresh(query: str) -> str:
    """Only the series of ``query`` with a sample from the last ``FRESH_SECONDS``."""
    return f"(time() - timestamp({query})) < {FRESH_SECONDS}"


def prom_value(query: str) -> float | None:
    """The first result's value, or None when the query returns nothing."""
    result = promql(query)
    return float(result[0]["value"][1]) if result else None


def loki(query: str, since: float | None = None) -> list[dict[str, Any]]:
    """Log lines since ``since`` (epoch seconds; default an hour ago), through Grafana's
    proxy (Loki publishes no port)."""
    start = time.time() - 3600 if since is None else since
    params = urllib.parse.urlencode({"query": query, "limit": 20, "start": str(int(start * 1e9))})
    url = f"{GRAFANA}/api/datasources/proxy/uid/loki/loki/api/v1/query_range?{params}"
    status, body = request(url)
    if status != 200:
        raise ValueError(f"Loki through Grafana answered HTTP {status}")
    streams = json.loads(body)["data"]["result"]
    return [json.loads(line) for stream in streams for _, line in stream["values"]]


def grafana_rules() -> list[dict[str, Any]]:
    """The Grafana-managed rules with their state and instances."""
    _, body = request(f"{GRAFANA}/api/prometheus/grafana/api/v1/rules")
    return [rule for group in json.loads(body)["data"]["groups"] for rule in group["rules"]]


def grafana_alert_state(alertname: str, labels: dict[str, str]) -> str:
    """firing, pending or inactive: the instance of ``alertname`` carrying ``labels``."""
    states = [
        alert["state"].lower()
        for rule in grafana_rules()
        if rule["name"] == alertname
        for alert in rule.get("alerts", [])
        if all(alert["labels"].get(key) == wanted for key, wanted in labels.items())
    ]
    if any(state.startswith("alerting") for state in states):
        return "firing"
    return "pending" if any(state.startswith("pending") for state in states) else "inactive"


def rules_in_files() -> int:
    return sum(
        len(re.findall(r"^\s*- alert:", path.read_text(), re.M))
        for path in (REPO / "ops" / "observability" / "alerts").glob("*.rules.yaml")
    )


def sentry_items() -> list[dict[str, Any]]:
    """What the Sentry stand-in received (sentry_stub.py)."""
    _, body = request(f"{SENTRY_STUB}/items")
    items: list[dict[str, Any]] = json.loads(body)
    return items


def real_sentry() -> bool:
    """Whether the app reports to a real Sentry project (LAB_SENTRY_DSN at `up`)."""
    found = subprocess.run(
        [*COMPOSE, "exec", "-T", "api", "printenv", "SENTRY_DSN"],
        capture_output=True,
        text=True,
        check=False,
    )
    return found.returncode == 0 and found.stdout.strip() != STUB_DSN


def epoch(value: object) -> float:
    """A Sentry timestamp, epoch seconds or ISO 8601, as epoch seconds."""
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    return 0.0


def transactions(items: list[dict[str, Any]], since: float = 0.0) -> list[dict[str, Any]]:
    return [
        item["payload"]
        for item in items
        if item["type"] == "transaction"
        and isinstance(item["payload"], dict)
        and epoch(item["payload"].get("start_timestamp")) >= since
    ]


def error_events(items: list[dict[str, Any]], since: float = 0.0) -> list[dict[str, Any]]:
    return [
        item["payload"]
        for item in items
        if item["type"] == "event"
        and isinstance(item["payload"], dict)
        and item["payload"].get("level") == "error"
        and epoch(item["payload"].get("timestamp")) >= since
    ]


def send_sentry_alert(event_id: str, *, age_seconds: float = 0, signed: bool = True) -> int:
    """A Sentry issue alert as Sentry's internal integration delivers it to the relay."""
    occurred = time.time() - age_seconds
    payload = {
        "action": "triggered",
        "data": {
            "event": {
                "event_id": event_id,
                "issue_id": "lab-issue",
                "level": "error",
                "title": "LabError: lab_drill",
                "timestamp": occurred,
                "datetime": datetime.fromtimestamp(occurred, UTC).isoformat(),
                "tags": [["environment", "lab"], ["release", "lab"]],
                "web_url": "https://sentry.example/issues/lab-issue/",
            },
            "triggered_rule": "Lab drill",
        },
    }
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "Sentry-Hook-Resource": "event_alert"}
    if signed:
        digest = hmac.new(SENTRY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        headers["Sentry-Hook-Signature"] = digest
    status, _ = request(RELAY, method="POST", body=body, headers=headers)
    return status


def loki_labels() -> set[str]:
    _, body = request(f"{GRAFANA}/api/datasources/proxy/uid/loki/loki/api/v1/labels")
    return set(json.loads(body).get("data") or [])


#: The last error a poll swallowed, for the detail of a failed check.
last_error = "none"


def poll[T](probe: Callable[[], T], done: Callable[[T], bool], timeout: float) -> T | None:
    """Calls ``probe`` every 3 s until ``done`` accepts its result or ``timeout`` runs
    out; returns the last result, or None if every call failed."""
    global last_error
    deadline = time.monotonic() + timeout
    result: T | None = None
    while True:
        try:
            result = probe()
        except (OSError, ValueError, KeyError) as error:
            # Not up yet, or nothing to parse yet.
            last_error = f"{type(error).__name__}: {error}"
        else:
            if done(result):
                return result
        if time.monotonic() >= deadline:
            return result
        time.sleep(3)


# --- the checks --------------------------------------------------------------------


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


class Report:
    def __init__(self) -> None:
        self.failed: list[str] = []
        self.total = 0

    def check(self, ok: bool, name: str, detail: str = "") -> bool:
        """Prints the check; ``detail`` (what was seen) only when it failed."""
        self.total += 1
        if ok:
            print(f"  ok    {name}", flush=True)
        else:
            self.failed.append(name)
            print(f"  FAIL  {name}{f'  ({detail})' if detail else ''}", flush=True)
        return ok

    def summary(self) -> bool:
        step("Result")
        if self.failed:
            print(f"  {len(self.failed)} of {self.total} checks failed:")
            for name in self.failed:
                print(f"    - {name}")
            return False
        print(f"  all {self.total} checks passed")
        return True


def check() -> bool:
    report = Report()
    started = time.time()
    run_id = f"lab-{int(started)}"
    # A content field: it must reach no log line (AGENTS.md hard rule 3).
    title = f"title {run_id} must not be logged"

    step("API and Caddy")
    try:
        health, _ = request(f"{API}/health")
    except OSError:
        print("  The lab is not running: uv run deploy/lab/lab.py up")
        return False
    report.check(health == 200, "API answers /health", f"HTTP {health}")
    # Caddy may still be signing its localhost certificate.
    edge = poll(lambda: request(f"{CADDY}/api/health")[0], lambda status: status == 200, 30)
    report.check(edge == 200, "Caddy proxies /api/health", f"HTTP {edge}")
    status, _ = request(f"{CADDY}/api/metrics")
    report.check(status == 404, "metrics are not public: /api/metrics is 404", f"HTTP {status}")

    step("Webhooks -> webhook_deliveries_total on :9100")
    api_before, worker_before = scrape(API_METRICS), scrape(WORKER_METRICS)
    # In this order: duplicate resends the delivery enqueued just before it.
    statuses = {
        outcome: deliver(
            outcome,
            run_id if outcome in ("enqueued", "duplicate") else f"{run_id}-{outcome}",
            title=title,
        )
        for outcome in EXPECTED_STATUS
    }
    api_after = scrape(API_METRICS)
    for outcome, expected in EXPECTED_STATUS.items():
        delta = value(api_after, "webhook_deliveries_total", outcome=outcome) - value(
            api_before, "webhook_deliveries_total", outcome=outcome
        )
        report.check(
            statuses[outcome] == expected and delta == 1,
            f"{outcome}: HTTP {expected}, counter +1",
            f"HTTP {statuses[outcome]}, counter +{delta:g}",
        )

    route = {"route": "/webhooks/github", "method": "POST"}
    by_class = {
        status_class: value(api_after, "http_requests_total", **route, status_class=status_class)
        - value(api_before, "http_requests_total", **route, status_class=status_class)
        for status_class in ("2xx", "4xx")
    }
    report.check(
        by_class == {"2xx": 3, "4xx": 3},
        "http_requests_total by status class: 2xx +3, 4xx +3",
        ", ".join(f"{k} +{v:g}" for k, v in by_class.items()),
    )
    timed = value(api_after, "http_request_duration_seconds_count", **route) - value(
        api_before, "http_request_duration_seconds_count", **route
    )
    report.check(timed == 6, "http_request_duration_seconds: 6 requests timed", f"+{timed:g}")
    report.check(
        not any(name.endswith("_created") for name, _, _ in api_after),
        "no _created series",
    )

    step("Worker on :9101 and the DB sampler")
    retrying = {"status": "RETRYING", "error_kind": "forge_unavailable"}

    def worker_delta(name: str, **labels: str) -> float:
        return value(scrape(WORKER_METRICS), name, **labels) - value(worker_before, name, **labels)

    retried = poll(lambda: worker_delta("jobs_finished_total", **retrying), bool, 30)
    report.check(
        bool(retried),
        "the job was claimed and retried: jobs_finished_total{RETRYING, forge_unavailable}",
        f"+{retried or 0:g}",
    )
    claimed = worker_delta("job_claim_latency_seconds_count")
    report.check(claimed >= 1, "job_claim_latency_seconds observed the claim", f"+{claimed:g}")
    # The job waits out its retry backoff as RETRYING; the sampler runs every 15 s.
    queued = poll(lambda: value(scrape(API_METRICS), "queue_jobs", status="RETRYING"), bool, 30)
    report.check(bool(queued), "DB sampler: queue_jobs{RETRYING} >= 1", f"{queued or 0:g}")
    report.check(
        has_series(scrape(API_METRICS), "queue_oldest_claimable_age_seconds"),
        "DB sampler: queue_oldest_claimable_age_seconds exported",
    )

    step("Alloy")
    components = (
        poll(
            lambda: json.loads(request(f"{ALLOY}/api/v0/web/components")[1]),
            lambda found: all(c.get("health", {}).get("state") == "healthy" for c in found),
            60,
        )
        or []
    )
    unhealthy = [c["localID"] for c in components if c.get("health", {}).get("state") != "healthy"]
    report.check(
        bool(components) and not unhealthy,
        f"all {len(components)} components healthy",
        ", ".join(unhealthy),
    )

    step(f"Prometheus, from Alloy's remote write (a scrape a minute; up to {PIPELINE_TIMEOUT} s)")
    up = (
        poll(
            lambda: {str(r["metric"]["job"]) for r in promql(fresh("up == 1"))},
            lambda jobs: jobs >= TARGETS,
            PIPELINE_TIMEOUT,
        )
        or set()
    )
    missing = sorted(TARGETS - up)
    report.check(not missing, f"all {len(TARGETS)} targets up", "missing: " + ", ".join(missing))

    # This run's values, not just old ones: wait until Prometheus has caught up.
    enqueued_now = value(api_after, "webhook_deliveries_total", outcome="enqueued")
    arrived = poll(
        lambda: prom_value('webhook_deliveries_total{outcome="enqueued"}'),
        lambda found: found is not None and found >= enqueued_now,
        PIPELINE_TIMEOUT,
    )
    report.check(
        arrived is not None and arrived >= enqueued_now,
        "this run's API counters arrived",
        f"enqueued {arrived} in Prometheus, {enqueued_now:g} on :9100",
    )
    for query, what in [
        ('jobs_finished_total{status="RETRYING"}', "worker: jobs_finished_total"),
        ('queue_jobs{status="RETRYING"}', "API: queue_jobs (DB sampler)"),
        ("caddy_http_requests_total", "Caddy: caddy_http_requests_total"),
        ("node_vmstat_oom_kill", "host: node_vmstat_oom_kill (the OOM alert)"),
        ("node_memory_MemAvailable_bytes", "host: node_memory_MemAvailable_bytes"),
        ("container_memory_working_set_bytes", "cadvisor: container_memory_working_set_bytes"),
        ("pg_up == 1", "postgres: pg_up == 1 (the exporter role works)"),
        ("pg_stat_statements_calls_total", "postgres: pg_stat_statements_calls_total"),
    ]:
        found = poll(lambda q=query: promql(fresh(q)), bool, 90)
        report.check(bool(found), what)
    report.check(
        not promql('{job=~"api|worker", __name__=~".+_created"}'),
        "no _created series in Prometheus",
    )
    series = prom_value("count({__name__=~'.+'})")
    print(f"  info  {series or 0:g} active series; Grafana Cloud's free tier allows 10,000")

    step("Loki, through Grafana (Alloy tails /logs/*.jsonl)")
    enqueued = poll(
        lambda: loki(f'{{service="api"}} | json | event="job.enqueued" | delivery_id="{run_id}"'),
        bool,
        60,
    )
    job_id = str(enqueued[0].get("job_id")) if enqueued else None
    report.check(
        bool(job_id),
        "the API's job.enqueued line, found by delivery_id",
        f"nothing found; last error: {last_error}",
    )
    worker_line = (
        poll(
            lambda: loki(f'{{service="worker"}} | json | event="job.retrying" | job_id="{job_id}"'),
            bool,
            60,
        )
        if job_id
        else None
    )
    report.check(
        bool(worker_line),
        "the same job in the worker's job.retrying line, by job_id",
        f"nothing found; last error: {last_error}" if job_id else "no job id to look for",
    )
    labels = poll(loki_labels, bool, 30) or set()
    extra = sorted(labels - LOKI_LABELS)
    report.check(
        bool(labels) and not extra,
        "index labels are low-cardinality: " + ", ".join(sorted(LOKI_LABELS)),
        "also: " + ", ".join(extra) if extra else f"no labels; last error: {last_error}",
    )
    leaked = poll(lambda: loki(f'{{service=~"api|worker"}} |= "{title}"'), lambda _: True, 30)
    report.check(
        leaked == [],
        "the PR title reached no log line",
        f"{len(leaked)} lines" if leaked else f"Loki did not answer; last error: {last_error}",
    )

    step("The Sentry relay (no bot, so alerts end as alert.dropped)")
    relay_event = f"{run_id}-alert"
    status = send_sentry_alert(relay_event)
    report.check(status == 202, "a signed Sentry alert: HTTP 202", f"HTTP {status}")
    dropped_line = poll(
        lambda: loki(
            '{service="api"} | json | event="alert.dropped" | issue_id="lab-issue"', started
        ),
        bool,
        60,
    )
    report.check(bool(dropped_line), "Loki: alert.dropped (the NullNotifier placeholder)")
    status = send_sentry_alert(f"{run_id}-unsigned", signed=False)
    report.check(status == 401, "an unsigned one: HTTP 401", f"HTTP {status}")
    stale_before = api_value("alert_relay_dropped_total", reason="stale")
    status = send_sentry_alert(f"{run_id}-stale", age_seconds=3600)
    stale = api_value("alert_relay_dropped_total", reason="stale") - stale_before
    report.check(
        status == 202 and stale == 1,
        'an hour-old one: dropped, alert_relay_dropped_total{reason="stale"} +1',
        f"HTTP {status}, +{stale:g}",
    )

    if real_sentry():
        step("Sentry: the app reports to your project (LAB_SENTRY_DSN); look there")
        print('  info  environment "lab", release "lab"')
    else:
        step("Sentry, the stand-in on :9900 (production's scrubbing: ENV is not local)")
        # The SDK sends from a background thread, within a few seconds.
        webhook = poll(
            lambda: [
                t
                for t in transactions(sentry_items(), started)
                if t.get("transaction") == "/webhooks/github"
            ],
            lambda found: len(found) >= len(EXPECTED_STATUS),
            30,
        ) or []  # fmt: skip
        report.check(
            len(webhook) >= len(EXPECTED_STATUS),
            f"a transaction per webhook request ({len(EXPECTED_STATUS)})",
            f"{len(webhook)} found",
        )
        report.check(
            bool(webhook)
            and all(t.get("environment") == "lab" and t.get("release") == "lab" for t in webhook),
            'environment "lab" and release "lab" on them',
        )
        report.check(
            all(
                "x-hub-signature-256"
                not in {h.lower() for h in t.get("request", {}).get("headers", {})}
                and "data" not in t.get("request", {})
                for t in webhook
            ),
            "no signature header and no body in them",
        )
        items = sentry_items()
        names = {t.get("transaction") for t in transactions(items)}
        report.check(
            not names & {"/health", RELAY_PATH},
            "no transaction for /health or the relay route",
            ", ".join(sorted(str(n) for n in names & {"/health", RELAY_PATH})),
        )
        report.check(title not in json.dumps(items), "the PR title reached no Sentry payload")
        # Phase 5: the review continues the webhook's trace (trace_context, migration 0004).
        reviews = poll(
            lambda: [
                t
                for t in transactions(sentry_items(), started)
                if t.get("transaction") == "review.job"
            ],
            bool,
            30,
        ) or []  # fmt: skip
        webhook_traces = {t["contexts"]["trace"]["trace_id"] for t in webhook}
        report.check(
            bool(reviews)
            and any(t["contexts"]["trace"]["trace_id"] in webhook_traces for t in reviews),
            "the claimed job is a review.job transaction in its webhook's trace",
            f"{len(reviews)} review.job transactions",
        )
        steps = {
            span.get("description")
            for t in reviews
            for span in t.get("spans", [])
            if span.get("op") == "review.step"
        }
        report.check(
            {"claimed", "fetch_diff"} <= steps,
            "its steps are spans: claimed, fetch_diff",
            ", ".join(sorted(str(step) for step in steps)),
        )

    step("Grafana: production's dashboards and alert rules (ops/observability/)")
    expected = rules_in_files()
    loaded = poll(
        grafana_rules,
        lambda found: len(found) == expected and all(r.get("health") == "ok" for r in found),
        90,
    ) or []  # fmt: skip
    sick = sorted({r["name"] for r in loaded if r.get("health") != "ok"})
    report.check(
        len(loaded) == expected and not sick,
        f"all {expected} alert rules loaded and evaluating",
        f"{len(loaded)} loaded; unhealthy: {', '.join(sick) or 'none'}",
    )
    _, body = request(f"{GRAFANA}/api/search?tag=dmc268&type=dash-db")
    titles = sorted(item["title"] for item in json.loads(body))
    report.check(len(titles) == 4, "the 4 dashboards are in Grafana", ", ".join(titles))
    return report.summary()


# --- send and drills ---------------------------------------------------------------


#: What a drill does while the part is broken: nothing, or wait for the Grafana alert.
Hold = Callable[["Report"], None]


def lab_is_up() -> bool:
    try:
        status, _ = request(f"{API}/health")
    except OSError:
        status = 0
    if status != 200:
        print("The lab is not running (or the API is down): uv run deploy/lab/lab.py up")
    return status == 200


def send(outcome: str, count: int, first_pr: int) -> bool:
    """``count`` deliveries counted as ``outcome``. Each PR delivery is for a PR of its
    own (``first_pr`` on): a new delivery for a PR supersedes that PR's active job."""
    ok = True
    for index in range(count):
        delivery, pr = new_delivery(), first_pr + index
        if outcome == "duplicate":
            deliver("enqueued", delivery, pr=pr)
        status = deliver(outcome, delivery, pr=pr)
        expected = EXPECTED_STATUS[outcome]
        ok = ok and status == expected
        where = f" (PR {pr})" if outcome in ("enqueued", "duplicate") else ""
        mark = "ok  " if status == expected else "FAIL"
        print(f"  {mark}  {outcome}{where}: HTTP {status}, expected {expected}")
    return ok


def wait_in_prometheus(report: Report, query: str, what: str, since: float) -> None:
    """Waits for a sample of ``query`` taken after ``since``: one an earlier drill left
    would still look fresh."""
    found = poll(lambda: promql(f"timestamp({query}) > {since}"), bool, PIPELINE_TIMEOUT)
    report.check(bool(found), f"Prometheus: {what}", f"no sample since the drill began: {query}")


def wait_in_loki(report: Report, query: str, what: str, since: float) -> None:
    found = poll(lambda: loki(query, since), bool, 60)
    report.check(bool(found), f"Loki: {what}", f"nothing since the drill began; {last_error}")


def drill_worker_down(report: Report, hold: Hold) -> list[str]:
    """Stop the worker and queue three PRs: the backlog grows and the target goes down."""
    since = time.time()
    step("Stopping the worker")
    compose("stop", "worker")
    try:
        step("Queueing PRs 201-203")
        for pr in (201, 202, 203):
            report.check(deliver("enqueued", new_delivery(), pr=pr) == 202, f"PR {pr} enqueued")
        # The DB sampler runs every 15 s.
        queued = poll(lambda: api_value("queue_jobs", status="QUEUED"), lambda n: n >= 3, 30)
        report.check((queued or 0) >= 3, 'queue_jobs{status="QUEUED"} >= 3', f"{queued}")
        age = api_value("queue_oldest_claimable_age_seconds")
        report.check(age > 0, "queue_oldest_claimable_age_seconds > 0", f"{age}")
        step(f"Waiting for the worker's target to go down (up to {PIPELINE_TIMEOUT} s)")
        wait_in_prometheus(report, 'up{job="worker"} == 0', 'up{job="worker"} == 0', since)
        hold(report)
    finally:
        step("Starting the worker")
        compose("start", "worker")
    drained = poll(lambda: api_value("queue_jobs", status="QUEUED"), lambda n: n == 0, 60)
    report.check(drained == 0, "the queue drains once the worker is back", f"{drained} queued")
    return [
        "queue_jobs",
        "queue_oldest_claimable_age_seconds",
        'up{job="worker"}',
        "alert Queue backlog: queue_oldest_claimable_age_seconds > 600 and on()"
        " sum(increase(jobs_finished_total[10m])) == 0 (needs a stop of over 10 min)",
        "alert Target down: up == 0 for 2 min",
    ]


def drill_db_down(report: Report, hold: Hold) -> list[str]:
    """Stop Postgres: webhooks fail, the queue gauges vanish, the API and worker log it."""
    since = time.time()
    step("Stopping Postgres")
    compose("stop", "postgres")
    try:
        gone = poll(
            lambda: has_series(scrape(API_METRICS), "queue_jobs"), lambda present: not present, 30
        )
        report.check(gone is False, "the DB sampler removed the queue gauges (no stale value)")
        before = api_value("webhook_deliveries_total", outcome="error")
        status = deliver("enqueued", new_delivery(), pr=210)
        delta = api_value("webhook_deliveries_total", outcome="error") - before
        report.check(
            status == 500 and delta == 1,
            'a webhook fails: HTTP 500, webhook_deliveries_total{outcome="error"} +1',
            f"HTTP {status}, +{delta:g}",
        )
        if not real_sentry():
            check_error_event(report, since)
        api_lines = '{service="api"} | json | event='
        wait_in_loki(
            report, api_lines + '"metrics.sampler.failed"', "metrics.sampler.failed", since
        )
        wait_in_loki(
            report,
            '{service="worker"} | json | event="worker.claim.failed"',
            "worker.claim.failed",
            since,
        )
        wait_in_loki(
            report,
            '{service="api", level="error"}',
            "the API's error line with the exception (a Sentry event in production)",
            since,
        )
        step(f"Waiting for pg_up to drop (up to {PIPELINE_TIMEOUT} s)")
        wait_in_prometheus(report, "pg_up == 0", "pg_up == 0", since)
        hold(report)
    finally:
        step("Starting Postgres")
        compose("up", "-d", "--wait", "postgres")
    back = poll(lambda: has_series(scrape(API_METRICS), "queue_jobs"), bool, 45)
    report.check(bool(back), "the queue gauges are back")
    wait_in_loki(
        report,
        '{service="api"} | json | event="metrics.sampler.recovered"',
        "metrics.sampler.recovered",
        since,
    )
    before = worker_value("jobs_finished_total", status="RETRYING")
    deliver("enqueued", new_delivery(), pr=211)
    claimed = poll(
        lambda: worker_value("jobs_finished_total", status="RETRYING") - before, bool, 60
    )
    report.check(bool(claimed), "the worker claims jobs again")
    return [
        'webhook_deliveries_total{outcome="error"}',
        "pg_up",
        "queue_jobs (a gap while Postgres was down)",
        '{level=~"warning|error"}  (Loki)',
        "alert Target down: pg_up == 0 for 2 min",
        "SLO Webhook ingest: the error deliveries count against it",
    ]


def check_error_event(report: Report, since: float) -> None:
    """The failed webhook as Sentry gets it: one error event, its message scrubbed."""
    events = poll(
        lambda: [
            event
            for event in error_events(sentry_items(), since)
            if "/webhooks/github"
            in str(event.get("transaction", "")) + str(event.get("request", {}).get("url", ""))
        ],
        bool,
        30,
    ) or []  # fmt: skip
    report.check(
        bool(events)
        and all(e.get("environment") == "lab" and e.get("release") == "lab" for e in events),
        'Sentry: the 500 is an error event, environment "lab", release "lab"',
        f"{len(events)} events",
    )
    values = [v.get("value") for e in events for v in e.get("exception", {}).get("values", [])]
    report.check(
        bool(values) and all(value == "" for value in values),
        "Sentry: the exception keeps its type, its message becomes the (empty) error_kind",
        f"{values}",
    )


def relay(*, stale: bool) -> bool:
    """One signed Sentry alert to the relay, as Sentry's internal integration sends it."""
    status = send_sentry_alert(new_delivery(), age_seconds=3600 if stale else 0)
    print(f"  HTTP {status}{' (stale: the relay drops it)' if stale else ''}")
    print('  Loki: {service="api"} | json | event=~"alert.*"')
    print("  Metrics: curl -s localhost:9100/metrics | grep alert_relay")
    return status == 202


EDGE_502 = 'sum(caddy_http_request_duration_seconds_count{code="502"})'


def drill_api_down(report: Report, hold: Hold) -> list[str]:
    """Stop the API: Caddy answers 502, which only Caddy's metrics can count."""
    since = time.time()
    step("Stopping the API")
    compose("stop", "api")
    try:
        before = prom_value(EDGE_502) or 0
        statuses = [request(f"{CADDY}/api/health")[0] for _ in range(5)]
        report.check(statuses == [502] * 5, "Caddy answers 502 for /api/health", f"{statuses}")
        step(f"Waiting for Prometheus (up to {PIPELINE_TIMEOUT} s)")
        arrived = poll(
            lambda: prom_value(EDGE_502) or 0, lambda n: n >= before + 5, PIPELINE_TIMEOUT
        )
        report.check(
            (arrived or 0) >= before + 5,
            'caddy_http_request_duration_seconds_count{code="502"} +5',
            f"{before:g} -> {arrived}",
        )
        wait_in_prometheus(report, 'up{job="api"} == 0', 'up{job="api"} == 0', since)
        hold(report)
    finally:
        step("Starting the API")
        compose("up", "-d", "--wait", "api")
    report.check(request(f"{API}/health")[0] == 200, "the API is back")
    return [
        'caddy_http_request_duration_seconds_count{code="502"}',
        "caddy_http_request_errors_total",
        'up{job="api"}',
        "queue_jobs (a gap: the DB sampler runs in the API)",
        'alert Edge 5xx: sum(rate(caddy_http_request_duration_seconds_count{code=~"5.."}[10m]))'
        " / sum(rate(caddy_http_request_duration_seconds_count[10m])) > 0.05",
        "alert Target down: up == 0 for 2 min",
        "runbook: GitHub's deliveries during the outage are lost; redeliver them",
    ]


def drill_alloy_down(report: Report, hold: Hold) -> list[str]:
    """Stop Alloy: nothing new reaches Prometheus or Loki."""
    step("Stopping Alloy")
    compose("stop", "alloy")
    try:
        step(f"Waiting for the last samples to go stale ({FRESH_SECONDS} s or more)")
        stale = poll(
            lambda: promql(fresh('up{job="api"}')), lambda found: not found, FRESH_SECONDS + 60
        )
        report.check(stale == [], f'no sample of up{{job="api"}} for {FRESH_SECONDS} s')
        hold(report)
    finally:
        step("Starting Alloy")
        compose("start", "alloy")
    back = poll(lambda: promql(fresh('up{job="api"} == 1')), bool, PIPELINE_TIMEOUT)
    report.check(bool(back), "samples arrive again")
    return [
        'up{job="api"}  (a gap of a few minutes)',
        'alert Telemetry missing: absent_over_time(up{job="api"}[5m]) (needs a stop of over 5 min)',
    ]


def drill_oom(report: Report, hold: Hold) -> list[str]:
    """Run a throwaway container past its memory limit: the kernel's OOM counter rises."""
    before = poll(lambda: prom_value("node_vmstat_oom_kill"), lambda n: n is not None, 90)
    report.check(before is not None, "Prometheus has node_vmstat_oom_kill")
    step("A container that asks for 512 MB under a 64 MB limit")
    # The app image's base, so it is already pulled.
    killed = subprocess.run(
        ["docker", "run", "--rm", "-m", "64m", "--memory-swap", "64m", "python:3.13-slim",
         "python", "-c", "bytearray(512 * 1024 * 1024)"],
        check=False,
    ).returncode  # fmt: skip
    report.check(killed == 137, "the kernel killed it (exit 137)", f"exit {killed}")
    step(f"Waiting for Prometheus (up to {PIPELINE_TIMEOUT} s)")
    after = poll(
        lambda: prom_value("node_vmstat_oom_kill") or 0,
        lambda n: n > (before or 0),
        PIPELINE_TIMEOUT,
    )
    report.check((after or 0) > (before or 0), "node_vmstat_oom_kill +1", f"{before} -> {after}")
    hold(report)
    return [
        "node_vmstat_oom_kill  (on Docker Desktop, the kernel of Docker's VM)",
        "alert OOM kill: increase(node_vmstat_oom_kill[15m]) > 0",
    ]


WORKER_START = 'container_start_time_seconds{name="dmc268-lab-worker-1"}'


def drill_restart(report: Report, hold: Hold) -> list[str]:
    """Restart the worker container: cadvisor's start time changes."""
    before = poll(lambda: prom_value(WORKER_START), lambda n: n is not None, 90)
    report.check(before is not None, "Prometheus has the worker's container_start_time_seconds")
    step("Restarting the worker")
    compose("restart", "worker")
    step(f"Waiting for Prometheus (up to {PIPELINE_TIMEOUT} s)")
    after = poll(
        lambda: prom_value(WORKER_START), lambda n: (n or 0) > (before or 0), PIPELINE_TIMEOUT
    )
    report.check((after or 0) > (before or 0), "a new start time", f"{before} -> {after}")
    return [
        "container_start_time_seconds",
        "alert Container restarts: > 2 in 15 min,"
        " e.g. changes(container_start_time_seconds[15m]) > 2",
    ]


DRILLS: dict[str, Callable[[Report, Hold], list[str]]] = {
    "worker-down": drill_worker_down,
    "db-down": drill_db_down,
    "api-down": drill_api_down,
    "alloy-down": drill_alloy_down,
    "oom": drill_oom,
    "restart": drill_restart,
}


#: The Grafana alert each drill rehearses, and the labels that pick its instance. The
#: restart drill has none: Container restarts needs three restarts in 15 minutes.
DRILL_ALERTS: dict[str, tuple[str, dict[str, str]]] = {
    "worker-down": ("TargetDown", {"job": "worker"}),
    "db-down": ("TargetDown", {"job": "postgres"}),
    "api-down": ("TargetDown", {"job": "api"}),
    "alloy-down": ("TelemetryMissing", {}),
    "oom": ("OomKill", {}),
}


def hold_until_alert(alertname: str, labels: dict[str, str]) -> Hold:
    def hold(report: Report) -> None:
        step(f"Holding until Grafana fires {alertname} (up to {ALERT_TIMEOUT // 60} min)")
        state = poll(
            lambda: grafana_alert_state(alertname, labels),
            lambda found: found == "firing",
            ALERT_TIMEOUT,
        )
        report.check(state == "firing", f"Grafana: {alertname} fires", f"state {state}")

    return hold


def no_hold(_: Report) -> None:
    return None


def drill(name: str, *, alert: bool = False) -> bool:
    if not lab_is_up():
        return False
    hold = no_hold
    if alert and name in DRILL_ALERTS:
        hold = hold_until_alert(*DRILL_ALERTS[name])
    elif alert:
        print(f"{name} has no alert to wait for: Container restarts needs three restarts.")
    report = Report()
    look = DRILLS[name](report, hold)
    step("Look in Grafana -> Explore, last 15 minutes (docs/observability.md § Alerts)")
    for line in look:
        print(f"  {line}")
    return report.summary()


def run_checks() -> bool:
    """``check``, with a crash reported as a failed check, so ``test`` keeps the lab."""
    try:
        return check()
    except Exception as error:
        print(f"  FAIL  the checks stopped: {type(error).__name__}: {error}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0], formatter_class=argparse.RawTextHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    test = commands.add_parser("test", help="up, check, down")
    test.add_argument("--keep", action="store_true", help="leave the lab running after the checks")
    commands.add_parser("up", help="build and start the lab")
    commands.add_parser("check", help="check a running lab; safe to repeat")
    commands.add_parser("down", help="remove the lab's containers and volumes")
    commands.add_parser("sync", help="reload the dashboards and alert rules into Grafana")
    relayer = commands.add_parser("relay", help="post a signed Sentry alert to the relay")
    relayer.add_argument("--stale", action="store_true", help="an hour old: the relay drops it")
    sender = commands.add_parser("send", help="send webhooks the API counts as OUTCOME")
    sender.add_argument("outcome", choices=EXPECTED_STATUS)
    sender.add_argument("--count", type=int, default=1, help="how many (default 1)")
    sender.add_argument("--pr", type=int, default=100, help="first PR number (default 100)")
    driller = commands.add_parser(
        "drill",
        help="break one part, check the signals, repair it",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    driller.add_argument(
        "name",
        choices=DRILLS,
        metavar="NAME",
        help="\n".join(f"{name}: {run.__doc__}" for name, run in DRILLS.items()),
    )
    driller.add_argument(
        "--alert",
        action="store_true",
        help="hold the part broken until its Grafana alert fires (up to 10 min)",
    )
    args = parser.parse_args()
    try:
        if args.command == "up":
            up()
            print(f"\nUp: {URLS}")
        elif args.command == "down":
            down()
        elif args.command == "check":
            return 0 if run_checks() else 1
        elif args.command == "send":
            return 0 if lab_is_up() and send(args.outcome, args.count, args.pr) else 1
        elif args.command == "drill":
            return 0 if drill(args.name, alert=args.alert) else 1
        elif args.command == "sync":
            sync_grafana()
        elif args.command == "relay":
            return 0 if lab_is_up() and relay(stale=args.stale) else 1
        else:
            if running():
                # Its `down` would remove a lab someone else is using or kept.
                print("The lab already exists: `lab.py check` checks it, `lab.py down` removes it.")
                return 1
            up()
            passed = run_checks()
            if args.keep or not passed:
                reason = "kept" if passed else "left running to look at what failed"
                print(f"\nThe lab is {reason}: {URLS}")
                print("Remove it with: uv run deploy/lab/lab.py down")
            else:
                down()
            return 0 if passed else 1
    except subprocess.CalledProcessError as error:
        print(f"\n{' '.join(error.cmd[len(COMPOSE) :])} failed (exit {error.returncode})")
        return 1
    except KeyboardInterrupt:
        # A drill has repaired what it broke by now (its finally block).
        print("\nInterrupted.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
