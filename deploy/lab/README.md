# Observability lab

The whole metrics and logs pipeline on a laptop
([docs/observability.md](../../docs/observability.md)):
- the API, the worker and Postgres as in production;
- Caddy with `deploy/Caddyfile`, and Alloy with `deploy/alloy/config.alloy`;
- local Prometheus, Loki and Grafana in place of Grafana Cloud, loaded with production's
  dashboards and alert rules (`ops/observability/`);
- a stand-in for Sentry's ingest (`sentry_stub.py`), which keeps what the app's Sentry
  SDK sends, and the Sentry → Telegram relay switched on, with no bot behind it.

It needs Docker and uv and no credentials, and it sends no data to Grafana Cloud, Sentry,
GitHub, Telegram or an LLM provider (unless you point it at a real Sentry project, below).

```bash
uv run deploy/lab/lab.py test           # build, start, check, remove
uv run deploy/lab/lab.py test --keep    # the same, then leave it running
uv run deploy/lab/lab.py up             # start only
uv run deploy/lab/lab.py check          # check a running lab; safe to repeat
uv run deploy/lab/lab.py down           # remove its containers and volumes
uv run deploy/lab/lab.py sync           # reload dashboards and rules after editing them
uv run deploy/lab/lab.py relay          # post a signed Sentry alert to the relay
```

The first run pulls about 4 GB of images; after that, `test` takes about a minute. `up`
runs the two manual steps production needs after the merge: the migration and the
`dmc268_exporter` role. Then it loads the dashboards, alert rules, mute timings and
notification policy into Grafana with `ops/observability/sync.py`, as the sync workflow
does for Grafana Cloud. The lab's `telegram` contact point is a webhook to a closed
port, so a firing alert shows in Grafana and goes nowhere.

`check` sends signed GitHub-style webhooks, one for every outcome except `error`. It
follows them through every hop:
- the counters on `:9100` and `:9101`;
- the job the worker claims and retries;
- the DB sampler's gauges;
- Alloy's components;
- all seven targets and this run's values in Prometheus;
- the same job in the API's and the worker's log lines in Loki;
- Caddy's and Postgres's lines in Loki, from Docker: with the API stopped for a moment,
  Caddy's error line for `/api/auth/callback?code=…` arrives as `?redacted`, and the
  code is in no line;
- the relay: a signed Sentry alert ends as `alert.dropped` (the placeholder for the
  bot), an unsigned one is 401, an hour-old one is dropped as stale and counted;
- Sentry: a transaction per webhook request, with environment and release `lab`, without
  the signature header or the body; none for `/health` or the relay; the PR title in no
  Sentry payload; the claimed job as a `review.job` transaction in its webhook's trace,
  with its steps as spans;
- every alert rule loaded and evaluating in Grafana, and the four dashboards there.

It also checks that metrics are not public through Caddy, that there are no `_created`
series, that Loki's index labels stay low-cardinality, and that the PR title reaches no
log line. It exits 1 when a check fails; `test` then leaves the lab running so you can
look. `test` refuses to start when the lab already exists, because its final `down`
would remove a lab that someone kept or is still checking: `check` it or `down` it
instead.

| Open | To see |
|---|---|
| http://localhost:3000 | Grafana: Dashboards → DMC-268 (the four dashboards); Alerting → Alert rules (state of every rule); Explore for raw queries, e.g. Loki `{service="worker"} \| json \| event="job.retrying"` |
| http://localhost:9090 | Prometheus: what Alloy pushed |
| http://localhost:12345 | Alloy: its components and their health |
| http://localhost:9900/items | What the app sent to Sentry: events and transactions, as JSON |
| http://localhost:9100/metrics, http://localhost:9101/metrics | The API's and the worker's own metrics |
| https://localhost:8443/api/health | The API through Caddy (its own certificate: `curl -k`) |

## What happens on its own

With no input, the lab produces:
- `GET /health` every 5 s from Docker's health check, counted in
  `http_requests_total{route="/health"}`. It is logged at debug level only, so it never
  reaches Loki.
- A `worker.reaper.tick` line every 30 s.
- The DB sampler's `queue_jobs` every 15 s; it logs only when a sample fails or
  recovers.
- Host, container and Postgres metrics from Alloy every 60 s.

Every PR job fails on purpose, because GitHub is a closed port here. It is retried after
about 1, 2 and 4 minutes and ends `FAILED` after about 7: an error-level `job.finished`
line, and `jobs_finished_total{status="FAILED", error_kind="forge_unavailable"}`.

## Sending webhooks

```bash
uv run deploy/lab/lab.py send enqueued --count 5   # PRs 100-104: five jobs
uv run deploy/lab/lab.py send duplicate            # a delivery, then the same one again
uv run deploy/lab/lab.py send filtered             # or unauthorized, rejected, too_large
```

Each delivery is counted in `webhook_deliveries_total` under its outcome. Each PR
delivery goes to a PR of its own (`--pr` sets the first number). A new delivery for a PR
supersedes that PR's active job, so deliveries for one PR never grow a queue.

## Drills

A drill breaks one part of a running lab, checks the signals an alert would read, and
repairs it. Then it prints what to query in Grafana → Explore and which alert from
[docs/observability.md § Alerts](../../docs/observability.md#alerts) it rehearses. With
`--alert`, it holds the part broken until that alert fires in Grafana (Alerting → Alert
rules shows it pending, then firing), which takes a scrape, an evaluation and the rule's
`for`: about 4 minutes for Target down, 6 for Telemetry missing.

```bash
uv run deploy/lab/lab.py up                         # or test --keep
uv run deploy/lab/lab.py drill worker-down          # the signals, about a minute
uv run deploy/lab/lab.py drill worker-down --alert  # the same, until Target down fires
uv run deploy/lab/lab.py drill -h                   # the list
```

| Drill | What it does | What you see | Alert it rehearses |
|---|---|---|---|
| `worker-down` | Stops the worker, queues PRs 201–203, starts the worker again | `queue_jobs{status="QUEUED"}` at 3 and `queue_oldest_claimable_age_seconds` growing; `up{job="worker"}` at 0; the queue drains afterwards | Queue backlog, Target down |
| `db-down` | Stops Postgres, sends a PR, starts Postgres again | HTTP 500 and `webhook_deliveries_total{outcome="error"}`; the queue gauges disappear; `pg_up` at 0. In Loki: `metrics.sampler.failed`, `worker.claim.failed`, the API's error line with the exception (a Sentry event in production), then `metrics.sampler.recovered`. The worker claims again afterwards | Target down (`pg_up`), the Webhook ingest SLO |
| `api-down` | Stops the API, calls it through Caddy, starts it again | Caddy's 502s in `caddy_http_request_duration_seconds_count{code="502"}` and `caddy_http_request_errors_total`; `up{job="api"}` at 0; a gap in `queue_jobs`, because the sampler runs in the API | Edge 5xx, Target down |
| `alloy-down` | Stops Alloy until its last samples are over 2 min old, starts it again | A gap in every series | Telemetry missing |
| `oom` | Runs a throwaway container past a 64 MB memory limit | Exit 137 and `node_vmstat_oom_kill` +1 (on Docker Desktop, the counter of Docker's VM) | OOM kill |
| `restart` | Restarts the worker container | A new `container_start_time_seconds{name="dmc268-lab-worker-1"}` | Container restarts |

Most drills take under a minute and a half, mostly waiting for Alloy's once-a-minute
scrape; `alloy-down` takes about three. A drill repairs what it broke even when a check
fails or you stop it with Ctrl-C.

Without `--alert`, a drill keeps a part broken only until the signal shows. `--alert`
covers the root alerts and OOM kill; Queue backlog needs over 10 minutes and Container
restarts three restarts, so break the part by hand for those and watch Alerting → Alert
rules:

```bash
alias lab='docker compose -f deploy/lab/compose.yml'
lab stop worker      # or postgres, api, alloy
uv run deploy/lab/lab.py send enqueued --count 3
lab start worker
```

## Sentry

The app reports to the stand-in on :9900 with production's scrubbing (`ENV` is `lab`,
not `local`), so you can see what Sentry would get:

```bash
curl -s localhost:9900/items | python3 -m json.tool | less
uv run deploy/lab/lab.py drill db-down   # a webhook fails with 500: one error event
uv run deploy/lab/lab.py relay           # a Sentry alert to the relay: alert.dropped in Loki
uv run deploy/lab/lab.py relay --stale   # an hour old: dropped, alert_relay_dropped_total
```

`drill db-down` also checks the error event: environment and release `lab`, and the
exception keeps its type while its message becomes the `error_kind`.

To see the lab's errors in a real Sentry project instead, start the lab with its DSN.
The events carry environment `lab`, so they can be filtered out, but they do reach
Sentry, so use a test project if you have one:

```bash
read -rs LAB_SENTRY_DSN && export LAB_SENTRY_DSN   # paste the DSN; nothing is echoed
uv run deploy/lab/lab.py up
```

`check` then skips its Sentry checks and tells you to look in Sentry.

## How it differs from production

- **Grafana Cloud is replaced** by local Prometheus, Loki and Grafana, and Alloy pushes
  to them with dummy credentials.
- **Every port is published on localhost,** the metrics ports and Alloy's UI included.
  Production publishes only Caddy's.
- **GitHub calls go to a closed local port,** so every job ends as `forge_unavailable`
  and no model is called. The App key and the LLM key are throwaway values.
- **Sentry is a stand-in** that keeps envelopes and does nothing else: no grouping, no
  alert rules, no email. The relay gets its alerts from `lab.py relay`, not from Sentry.
- **Caddy serves `localhost` on 8443** with its own certificate, and serves no frontend.
- **No disk metrics on Docker Desktop:** its VM's root filesystem is `overlay`, which the
  filesystem collector skips, so Host resources sees only RAM there. On the VPS the root
  is a normal disk.
- **No external probe:** Synthetic Monitoring exists only in Grafana Cloud, so Site
  unreachable and TLS certificate expiry never fire here.
- **Loki and Grafana send no usage reports.** Alloy sends none in production either.

## Requirements

The Alloy, Caddy and Postgres images must match `deploy/compose.yml`: `lab.py` stops if
they differ. Ports 3000, 8000, 8443, 9090, 9100, 9101, 9900 and 12345 must be free. The repo's
`docker-compose.yml` uses 8000 too, so stop it first.
