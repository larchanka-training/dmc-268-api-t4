"""The four Grafana dashboards of docs/observability.md § Dashboards, as JSON.

The panels are defined here, compactly; the JSON next to this file is generated from
them and committed, so a review sees both. ops/observability/sync.py pushes the JSON.

    uv run python -m ops.observability.dashboards.build           # rewrite the JSON
    uv run python -m ops.observability.dashboards.build --check   # exit 1 if stale

Every panel queries the ``prometheus`` dashboard variable, so the same JSON works in
Grafana Cloud and in the lab (deploy/lab/). Panels on metrics a later phase adds (LLM,
forge rate limit, security events, steps, findings, redactions, canary) show "No data"
until then. Every dashboard shows the deploy annotations (tag ``deploy``).
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROMETHEUS = {"type": "prometheus", "uid": "${prometheus}"}
GRAFANA = {"type": "grafana", "uid": "-- Grafana --"}
#: The external probe's check (ops/observability/synthetics/uptime.json).
SITE_JOB = "dmc268-site"
NOT_HEALTH = 'route!="/health"'
#: Grafana's rate window, at least four scrapes.
W = "[$__rate_interval]"


@dataclass(frozen=True)
class Panel:
    title: str
    #: (expr, legend) pairs; a legend of "" lets Grafana name the series.
    queries: tuple[tuple[str, str], ...] = ()
    kind: str = "timeseries"
    unit: str | None = None
    description: str = ""
    width: int = 12
    stacked: bool = False
    #: stat: what the latest value means, e.g. {0: "down", 1: "up"}.
    mappings: dict[int, str] = field(default_factory=dict)
    links: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Dashboard:
    uid: str
    title: str
    description: str
    panels: tuple[Panel, ...]


def q(expr: str, legend: str = "") -> tuple[str, str]:
    return (expr, legend)


OVERVIEW = Dashboard(
    uid="dmc268-overview",
    title="DMC-268 Overview",
    description="Is the service healthy, at a glance. Detail on the other three dashboards.",
    panels=(
        Panel(
            "Requests per second",
            (q(f"sum(rate(http_requests_total{{{NOT_HEALTH}}}{W}))", "API"),),
            unit="reqps",
            width=8,
        ),
        Panel(
            "API error rate (5xx)",
            (
                q(
                    f'sum(rate(http_requests_total{{{NOT_HEALTH}, status_class="5xx"}}{W}))'
                    f" / sum(rate(http_requests_total{{{NOT_HEALTH}}}{W}))",
                    "5xx share",
                ),
            ),
            unit="percentunit",
            width=8,
            description="The alert fires above 5% over 10 minutes with at least 20 requests.",
        ),
        Panel(
            "API latency p95",
            (
                q(
                    "histogram_quantile(0.95, sum by (le) "
                    f"(rate(http_request_duration_seconds_bucket{{{NOT_HEALTH}}}{W})))",
                    "p95",
                ),
            ),
            unit="s",
            width=8,
        ),
        Panel(
            "Queue depth",
            (q("queue_jobs", "{{status}}"),),
            width=8,
            stacked=True,
            description="From the API's DB sampler, every 15 s; absent when a sample fails.",
        ),
        Panel(
            "Oldest claimable job",
            (q("queue_oldest_claimable_age_seconds", "age"),),
            unit="s",
            width=8,
            description="A job in its retry backoff is not counted. Queue backlog fires above "
            "10 minutes while nothing finishes.",
        ),
        Panel(
            "Jobs finished per hour, by status",
            (q("sum by (status) (increase(jobs_finished_total[1h]))", "{{status}}"),),
            width=8,
            stacked=True,
            description="RETRYING is one attempt; COMPLETED and FAILED are jobs.",
        ),
        Panel(
            "LLM cost, last 24 h",
            (q("sum(increase(llm_cost_usd_total[1d]))", "USD"),),
            kind="stat",
            unit="currencyUSD",
            width=8,
            description="From phase 5.",
        ),
        Panel(
            "External probe",
            (q(f'min by (probe) (probe_success{{job="{SITE_JOB}"}})', "{{probe}}"),),
            kind="stat",
            width=8,
            mappings={0: "down", 1: "up"},
            description="Grafana Synthetic Monitoring, every 2 minutes from three EU locations.",
        ),
        Panel("Firing alerts", kind="alertlist", width=8),
    ),
)

API = Dashboard(
    uid="dmc268-api",
    title="DMC-268 API",
    description="Traffic, latency and refusals of the API, and the edge in front of it.",
    panels=(
        Panel(
            "Requests per second by route",
            (q("sum by (route) (rate(http_requests_total[$__rate_interval]))", "{{route}}"),),
            unit="reqps",
            stacked=True,
        ),
        Panel(
            "Latency p95 by route",
            (
                q(
                    "histogram_quantile(0.95, sum by (le, route) "
                    "(rate(http_request_duration_seconds_bucket[$__rate_interval])))",
                    "{{route}}",
                ),
            ),
            unit="s",
            description="The SSE stream is not observed. The data link opens the route's "
            "traces in Sentry (dashboard variable sentry_url).",
            links=(
                (
                    "Traces of ${__field.labels.route} in Sentry",
                    "${sentry_url}/explore/traces/?query=transaction%3A%22"
                    "${__field.labels.route}%22&start=${__from:date:iso}&end=${__to:date:iso}",
                ),
            ),
        ),
        Panel(
            "Status classes",
            (
                q(
                    f"sum by (status_class) (rate(http_requests_total{{{NOT_HEALTH}}}{W}))",
                    "{{status_class}}",
                ),
            ),
            unit="reqps",
            stacked=True,
        ),
        Panel(
            "Webhook outcomes",
            (
                q(
                    f"sum by (provider, outcome) (increase(webhook_deliveries_total{W}))",
                    "{{provider}} {{outcome}}",
                ),
            ),
            stacked=True,
            description="rejected: a signed delivery we could not parse; error: we answered "
            "5xx. GitHub retries neither.",
        ),
        Panel(
            "5xx at the edge (Caddy)",
            (
                q(
                    "sum by (code) (rate(caddy_http_request_duration_seconds_count"
                    '{code=~"5.."}[$__rate_interval]))',
                    "{{code}}",
                ),
                q("sum(rate(caddy_http_request_errors_total[$__rate_interval]))", "handler errors"),
            ),
            unit="reqps",
            description="Includes the 502s Caddy answers while the API is down, which the API "
            "never sees.",
        ),
        Panel(
            "Security events by kind",
            (q("sum by (kind) (increase(security_events_total[$__rate_interval]))", "{{kind}}"),),
            stacked=True,
            description="From phase 5.",
        ),
    ),
)

PIPELINE = Dashboard(
    uid="dmc268-pipeline",
    title="DMC-268 Pipeline",
    description="From webhook to posted review: durations, failures, the LLM and the forge.",
    panels=(
        Panel(
            "Review duration (enqueue to done)",
            tuple(
                q(
                    f"histogram_quantile({quantile}, sum by (le) "
                    '(rate(review_duration_seconds_bucket{status="COMPLETED"}[1h])))',
                    name,
                )
                for quantile, name in ((0.5, "p50"), (0.95, "p95"))
            ),
            unit="s",
            description="Queue wait and earlier attempts included. The latency SLO is 95% "
            "within 10 minutes.",
        ),
        Panel(
            "Step duration (mean) by step",
            (
                q(
                    "sum by (step) (rate(review_step_duration_seconds_sum[1h]))"
                    " / sum by (step) (rate(review_step_duration_seconds_count[1h]))",
                    "{{step}}",
                ),
            ),
            unit="s",
            stacked=True,
            description="From phase 5.",
        ),
        Panel(
            "Claim latency p95",
            (
                q(
                    "histogram_quantile(0.95, sum by (le) "
                    "(rate(job_claim_latency_seconds_bucket[1h])))",
                    "p95",
                ),
            ),
            unit="s",
            description="Claimable to claimed; a retry's backoff does not count.",
        ),
        Panel(
            "Failures by error_kind",
            (
                q(
                    "sum by (status, error_kind) "
                    '(increase(jobs_finished_total{status!="COMPLETED"}[1h]))',
                    "{{status}} {{error_kind}}",
                ),
            ),
            stacked=True,
            description="forge_unavailable and provider_unavailable retry; the rest fail.",
        ),
        Panel(
            "LLM requests by provider and outcome",
            (
                q(
                    "sum by (provider, outcome) (increase(llm_requests_total[1h]))",
                    "{{provider}} {{outcome}}",
                ),
            ),
            stacked=True,
            description="From phase 5.",
        ),
        Panel(
            "LLM tokens and cost per model, last 24 h",
            (
                q("sum by (model) (increase(llm_tokens_total[1d]))", "{{model}} tokens"),
                q("sum by (model) (increase(llm_cost_usd_total[1d]))", "{{model}} USD"),
            ),
            description="From phase 5.",
        ),
        Panel(
            "Forge rate-limit headroom",
            (q("min by (provider) (forge_rate_limit_remaining_ratio)", "{{provider}}"),),
            unit="percentunit",
            description="From phase 5. The alert fires under 10% for 10 minutes.",
        ),
        Panel(
            "Findings posted and secrets redacted, per hour",
            (
                q("sum by (severity) (increase(findings_posted_total[1h]))", "{{severity}}"),
                q("sum by (rule) (increase(secrets_redacted_total[1h]))", "redacted {{rule}}"),
            ),
            description="From phase 5.",
        ),
        Panel(
            "Since the last canary review",
            (q("time() - canary_review_last_success_timestamp", "age"),),
            kind="stat",
            unit="s",
            description="From phase 6. The alert fires after 4 h 15 min.",
        ),
        Panel(
            "Jobs in flight",
            (q("sum(worker_in_flight)", "in flight"),),
            description="Concurrency used, against WORKER_CONCURRENCY.",
        ),
    ),
)

INFRASTRUCTURE = Dashboard(
    uid="dmc268-infrastructure",
    title="DMC-268 Infrastructure",
    description="The VPS, its containers, Postgres, the certificate and Alloy itself.",
    panels=(
        Panel(
            "Host CPU, RAM and disk",
            (
                q('1 - avg(rate(node_cpu_seconds_total{mode="idle"}[$__rate_interval]))', "CPU"),
                q("1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes", "RAM"),
                q(
                    '1 - node_filesystem_avail_bytes{mountpoint="/"}'
                    ' / node_filesystem_size_bytes{mountpoint="/"}',
                    "disk",
                ),
            ),
            unit="percentunit",
            description="Host resources fires at RAM over 90% or disk over 85% for 10 minutes.",
        ),
        Panel(
            "OOM kills",
            (q("increase(node_vmstat_oom_kill[$__rate_interval])", "kills"),),
            description="The kernel's counter. Container restarts names the container.",
        ),
        Panel(
            "Container CPU",
            (
                q(
                    f'sum by (name) (rate(container_cpu_usage_seconds_total{{name!=""}}{W}))',
                    "{{name}}",
                ),
            ),
            unit="percentunit",
            width=8,
        ),
        Panel(
            "Container memory",
            (q('sum by (name) (container_memory_working_set_bytes{name!=""})', "{{name}}"),),
            unit="bytes",
            width=8,
        ),
        Panel(
            "Container restarts, last hour",
            (q('changes(container_start_time_seconds{name!=""}[1h])', "{{name}}"),),
            width=8,
        ),
        Panel(
            "Postgres connections",
            (
                q("sum by (state) (pg_stat_activity_count)", "{{state}}"),
                q("max(pg_settings_max_connections)", "max_connections"),
            ),
            width=8,
            stacked=False,
        ),
        Panel(
            "Table sizes",
            (q("sum by (relname) (pg_stat_user_tables_table_size_bytes)", "{{relname}}"),),
            unit="bytes",
            width=8,
        ),
        Panel(
            "Dead tuples",
            (q("sum by (relname) (pg_stat_user_tables_n_dead_tup)", "{{relname}}"),),
            width=8,
            description="Queue table bloat fires on pr_review_jobs at 10 times the live rows "
            "with no autovacuum in 24 h.",
        ),
        Panel(
            "Top statements by time",
            (q("topk(10, sum by (queryid) (rate(pg_stat_statements_seconds_total[1h])))", ""),),
            kind="table",
            unit="s",
            description="Seconds per second over the last hour, by queryid; the text stays in "
            "Postgres (pg_stat_statements).",
        ),
        Panel(
            "TLS certificate expires in",
            (q(f'min(probe_ssl_earliest_cert_expiry{{job="{SITE_JOB}"}}) - time()', "left"),),
            kind="stat",
            unit="s",
            width=6,
            description="Caddy renews at 30 days left; the alert fires under 14.",
        ),
        Panel(
            "Alloy pushes",
            (
                q("sum(prometheus_remote_write_wal_storage_active_series)", "active series"),
                q(
                    "sum(rate(prometheus_remote_storage_samples_failed_total[$__rate_interval]))",
                    "failed samples/s",
                ),
                q(
                    "sum(rate(loki_write_dropped_entries_total[$__rate_interval]))",
                    "dropped log lines/s",
                ),
            ),
            width=6,
            description="Active series against the free tier's 10,000; Monitoring broken fires "
            "after 15 minutes of failures.",
        ),
    ),
)

DASHBOARDS = (OVERVIEW, API, PIPELINE, INFRASTRUCTURE)


# --- JSON --------------------------------------------------------------------------


def panel_json(panel: Panel, panel_id: int, x: int, y: int) -> dict[str, Any]:
    height = 6 if panel.kind == "stat" else 8
    result: dict[str, Any] = {
        "id": panel_id,
        "type": panel.kind,
        "title": panel.title,
        "description": panel.description,
        "gridPos": {"h": height, "w": panel.width, "x": x, "y": y},
    }
    if panel.kind == "alertlist":
        result["options"] = {
            "viewMode": "list",
            "groupMode": "default",
            "maxItems": 20,
            "sortOrder": 1,
            "dashboardAlerts": False,
            "alertName": "",
            "alertInstanceLabelFilter": "",
            "stateFilter": {"firing": True, "pending": True, "error": True},
        }
        return result
    defaults: dict[str, Any] = {}
    if panel.unit:
        defaults["unit"] = panel.unit
    if panel.kind == "timeseries":
        defaults["custom"] = {
            "stacking": {"mode": "normal" if panel.stacked else "none", "group": "A"},
            "fillOpacity": 20 if panel.stacked else 0,
        }
    if panel.mappings:
        defaults["mappings"] = [
            {"type": "value", "options": {str(k): {"text": v} for k, v in panel.mappings.items()}}
        ]
    if panel.links:
        defaults["links"] = [{"title": title, "url": url} for title, url in panel.links]
    result["datasource"] = PROMETHEUS
    result["fieldConfig"] = {"defaults": defaults, "overrides": []}
    result["targets"] = [
        {
            "refId": chr(ord("A") + index),
            "datasource": PROMETHEUS,
            "expr": expr,
            "legendFormat": legend,
            **({"instant": True, "format": "table"} if panel.kind == "table" else {}),
        }
        for index, (expr, legend) in enumerate(panel.queries)
    ]
    if panel.kind == "timeseries":
        result["options"] = {
            "legend": {"displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "multi", "sort": "desc"},
        }
    elif panel.kind == "stat":
        result["options"] = {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value",
            "graphMode": "area",
        }
    return result


def dashboard_json(dashboard: Dashboard) -> dict[str, Any]:
    panels, x, y, row_height = [], 0, 0, 0
    for panel_id, panel in enumerate(dashboard.panels, start=1):
        if x + panel.width > 24:
            x, y, row_height = 0, y + row_height, 0
        item = panel_json(panel, panel_id, x, y)
        panels.append(item)
        x += panel.width
        row_height = max(row_height, item["gridPos"]["h"])
    return {
        "uid": dashboard.uid,
        "title": dashboard.title,
        "description": dashboard.description,
        "tags": ["dmc268"],
        "timezone": "browser",
        "editable": True,
        "graphTooltip": 1,
        "schemaVersion": 39,
        "time": {"from": "now-6h", "to": "now"},
        "refresh": "1m",
        "links": [
            {"type": "dashboards", "tags": ["dmc268"], "asDropdown": False, "title": "DMC-268"}
        ],
        "annotations": {
            "list": [
                {
                    "builtIn": 1,
                    "datasource": GRAFANA,
                    "enable": True,
                    "hide": True,
                    "iconColor": "rgba(0, 211, 255, 1)",
                    "name": "Annotations & Alerts",
                    "type": "dashboard",
                },
                {
                    # Posted by deploy.yml after the smoke test, text = IMAGE_TAG.
                    "datasource": GRAFANA,
                    "enable": True,
                    "iconColor": "orange",
                    "name": "Deploys",
                    "target": {"type": "tags", "tags": ["deploy"], "matchAny": True, "limit": 100},
                },
            ]
        },
        "templating": {
            "list": [
                {
                    "name": "prometheus",
                    "label": "Prometheus",
                    "type": "datasource",
                    "query": "prometheus",
                    "current": {},
                    "hide": 0,
                    "refresh": 1,
                    "regex": "",
                    "options": [],
                },
                {
                    "name": "sentry_url",
                    "label": "Sentry",
                    "description": "The Sentry organisation URL, for the traces link",
                    "type": "constant",
                    "query": "https://sentry.io",
                    "hide": 2,
                },
            ]
        },
        "panels": panels,
    }


def render(dashboard: Dashboard) -> str:
    return json.dumps(dashboard_json(dashboard), indent=2, ensure_ascii=False) + "\n"


def path_of(dashboard: Dashboard) -> Path:
    return HERE / f"{dashboard.uid.removeprefix('dmc268-')}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Write, or check, the dashboard JSON.")
    parser.add_argument("--check", action="store_true", help="exit 1 if a file is stale")
    args = parser.parse_args()
    stale = []
    for dashboard in DASHBOARDS:
        path, text = path_of(dashboard), render(dashboard)
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(path.name)
        else:
            path.write_text(text, encoding="utf-8")
    if stale:
        print("stale, run python -m ops.observability.dashboards.build: " + ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
