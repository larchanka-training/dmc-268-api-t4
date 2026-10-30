# Runbooks

One section per alert of [observability.md § Alerts](./observability.md#alerts); each
rule's `runbook_url` points here. Every section says what the alert means, the first
three things to check, and how to silence it. The rules live in
`ops/observability/alerts/`; a Telegram message carries the alert's `domain`, `owner` and
this link.

**Shared steps:**
- **On the VPS:** `ssh` in, then `cd /opt/dmc268`.
  - `docker compose ps` shows what runs.
  - `docker compose logs --since 30m <service>` shows recent logs.
  - The rotated JSON logs are in `logs/api.jsonl` and `logs/worker.jsonl`.
- **In Grafana:** the dashboards are in the DMC-268 folder. In Explore → Loki,
  `{service="api"} | json | level="error"` finds the API's errors.
- **To silence** an alert for planned work: Grafana → Alerting → Silences → New silence.
  Match `alertname` (and `domain` if the name is shared, as with Target down), set a
  duration and a comment. Prefer a silence to pausing a rule: a silence expires on its
  own.
- **Owners:** `owner` is `unassigned` until the team assigns one person per domain
  (docs/tasks/observability-checklist.md).

## Target down

**Means:** a process answers no scrape for 2 minutes: the API (`domain=api`), the worker
(`worker`) or Postgres (`db`, from `pg_up`). It is a root alert: the alerts that depend
on it stay quiet, so this may be the only message for a whole outage.

**First checks:**
1. `docker compose ps`: is the container up, restarting, or gone?
2. `docker compose logs --since 30m <api|worker|postgres>`: the last lines before it
   stopped answering. Also check *Container restarts* and *OOM kill*.
3. If the container runs but the target is down: Alloy's UI from the VPS
   (`docker compose exec alloy wget -qO- localhost:12345/api/v0/web/components`), or the
   Infrastructure dashboard's Alloy panel.

**After an API outage:** GitHub does not retry a failed delivery, so reviews requested
during the outage are lost until redelivered. Open the App's settings → *Advanced* →
*Recent deliveries*, and redeliver each failed delivery since the outage began (or
`POST /app/hook/deliveries/{id}/attempts` with the App JWT). GitHub lists only the last
3 days. A delivery that did arrive is skipped as a duplicate (WORKFLOW_DESIGN §4).

**Silence:** `alertname=TargetDown` with `domain` for the one process, for the length of
the maintenance.

## Telemetry missing

**Means:** no sample of `up{job="api"}` reached Grafana Cloud for 5 minutes. Alloy
stopped, its push is failing, or the whole VPS is down. Everything else is blind while
it fires.

**First checks:**
1. *Site unreachable* firing as well: the VPS or Caddy is down; start there.
2. `docker compose ps alloy` and `docker compose logs --since 30m alloy`: look for
   remote-write errors, such as 401 after a token rotation.
3. The deploy summary: "Alloy not started" means a Grafana setting is missing in GitHub.

**Silence:** `alertname=TelemetryMissing`, only while Alloy is down on purpose.

## Site unreachable

**Means:** the external probe (Synthetic Monitoring, every 2 minutes from three EU
locations) failed `https://<site>/api/health` from at least 2 of them on both runs of
the last 4 minutes. It is measured entirely outside the VPS, so it fires when the VPS,
Caddy or the network is gone.

**First checks:**
1. Open `https://<site>/api/health` yourself, then the Synthetic Monitoring check's page:
   which locations fail, and with what (DNS, TLS, timeout, status code).
2. The VPS provider's console: is the server running?
3. `docker compose ps caddy` and `docker compose logs --since 30m caddy`: certificate
   errors, or Caddy not running.

**After the outage:** redeliver GitHub's failed deliveries as under *Target down*.

**Silence:** `alertname=SiteUnreachable` for planned VPS maintenance.

## API error rate

**Means:** over 5% of API requests (`/health` excluded) answered 5xx in 10 minutes, with
at least 20 requests. It catches 5xx answers that raise no exception, which Sentry never
sees.

**First checks:**
1. API dashboard → Status classes and RPS by route: which route fails.
2. Sentry: a new or regressed issue in the same minutes.
3. A `502` from `/repositories` is `forge_unavailable`, meaning GitHub is down. Check
   githubstatus.com before the code.

**Silence:** `alertname=ApiErrorRate` while a known upstream outage lasts.

## API latency

**Means:** API p95 latency was over 2 s for 10 minutes. GitHub fails a webhook that is
not answered within 10 s, and does not retry it.

**First checks:**
1. API dashboard → Latency p95 by route; the panel's link opens the slow route's traces
   in Sentry.
2. Infrastructure dashboard: host CPU, Postgres connections, top statements.
3. If `/webhooks/github` is slow: Recent deliveries in the App settings, to see whether
   GitHub has already marked deliveries failed (redeliver them).

**Silence:** `alertname=ApiLatency` during a planned load test.

## Edge 5xx

**Means:** over 5% of requests at Caddy answered 5xx in 10 minutes, with at least 20
requests. It is counted from `caddy_http_request_duration_seconds_count` by `code`. It
includes the 502s Caddy answers while the API is down, but stays quiet while *Target
down* (api) fires.

**First checks:**
1. API dashboard → 5xx at the edge: which code. 502 or 504 means the API didn't answer
   in time; other codes come from Caddy itself.
2. `docker compose logs --since 30m caddy`.
3. *API latency*: a slow API turns into 504s at the edge.

**Silence:** `alertname=Edge5xx` for planned work on Caddy.

## Relay failing

**Means:** the Sentry → Telegram relay failed to post to Telegram in the last 15
minutes. The admin still gets Sentry's email, but the team hears nothing.

**First checks:**
1. Loki: `{service="api"} | json | event="alert.relay_failed"`; its `error_type` says why.
2. `TelegramError` with HTTP 401 means the bot token was revoked; 400 or 403 means the
   bot was removed from the chat or the chat id is wrong.
3. Update `ALERTS_TELEGRAM_BOT_TOKEN` or `ALERTS_TELEGRAM_CHAT_ID` in GitHub Secrets and redeploy.

**Silence:** `alertname=RelayFailing` while the bot is being replaced.

## Host resources

**Means:** the VPS's RAM was over 90% or its disk over 85% used for 10 minutes.

**First checks:**
1. Infrastructure dashboard: which resource, and which container grows (container
   memory).
2. Disk: `df -h /` and `docker system df` on the VPS. Old images build up across
   deploys (`docker image prune`); the rotated logs keep 14 days.
3. Memory: *OOM kill* and *Container restarts* soon follow; find the container that
   grows.

**Silence:** `alertname=HostResources` for a planned migration or backup.

## Container restarts

**Means:** a container restarted more than twice in 15 minutes: it is crashing and
compose keeps restarting it. The alert's `name` label says which container.

**First checks:**
1. `docker compose logs --since 30m <service>`: the traceback before each start.
2. *OOM kill* at the same time: the kernel killed it for memory.
3. A deploy just before: roll back with `IMAGE_TAG=<old sha> docker compose up -d <service>`
   (devops.md).

**Silence:** `alertname=ContainerRestarts` while debugging a known crash.

## OOM kill

**Means:** the kernel killed a process for lack of memory in the last 15 minutes.
*Container restarts* names the container if it was restarted.

**First checks:**
1. `dmesg -T | grep -i "killed process"` on the VPS: which process.
2. Infrastructure dashboard → Container memory: who grew.
3. If it was the worker: the size of the diff it was reviewing (Loki,
   `{service="worker"} | json | event="job.finished"`).

**Silence:** `alertname=OomKill`, rarely: every kill is worth a look.

## Monitoring broken

**Means:** Alloy's pushes to Grafana Cloud kept failing for 15 minutes: metric samples
rejected, or log lines dropped. Data still flows, but parts are missing. A complete stop
is *Telemetry missing*.

**First checks:**
1. Infrastructure dashboard → Alloy pushes: metrics or logs?
2. `docker compose logs --since 30m alloy | grep -i error`. Look for 429 or "series
   limit" (the free tier's 10,000 series), or 400 "out of order".
3. Grafana Cloud → Usage: active series against the limit.

**Silence:** `alertname=MonitoringBroken` while the series budget is being fixed.

## TLS certificate expiry

**Means:** the probe sees the site's certificate expiring in under 14 days. Caddy renews
at 30 days left, so renewal is failing.

**First checks:**
1. `docker compose logs caddy | grep -i -E "acme|certificate"`: the renewal error.
2. Port 80 must be open from the internet for the HTTP-01 challenge.
3. Let's Encrypt's rate limits: losing the `caddy_data` volume means a new certificate on
   every deploy.

**Silence:** `alertname=TlsCertificateExpiry` only once renewal is fixed and the probe
has not caught up yet.

## Queue backlog

**Means:** a job has been claimable for over 10 minutes, and no job finished in 10
minutes. Work is waiting and nothing drains it. A wait alone is normal: at most 3 jobs
per installation run at once.

**First checks:**
1. Overview dashboard → Queue depth and Oldest claimable job; Pipeline → Jobs in flight.
2. Worker logs for `worker.claim.failed` or `worker.listener` errors:
   `docker compose logs --since 30m worker`.
3. Postgres: `docker compose exec postgres psql -U dmc -d dmc268 -c "select status, count(*) from pr_review_jobs group by 1"`.

**Silence:** `alertname=QueueBacklog` while the worker is stopped on purpose.

## Lease expiry

**Means:** the reaper took back an expired lease in the last hour. A worker died while
holding a job, or a review outlived its 600 s lease.

**First checks:**
1. *Container restarts* or *OOM kill* for the worker at the same time.
2. Pipeline dashboard → Review duration and Step duration: a slow LLM provider.
3. Loki: `{service="worker"} | json | event="worker.reaper.tick" | reaped > 0`.

**Silence:** `alertname=LeaseExpiry` for a known slow provider period.

## Queue table bloat

**Means:** `pr_review_jobs` has over 10 times more dead rows than live ones and no
autovacuum in 24 hours. Claims and the queue gauges slow down as it grows.

**First checks:**
1. Infrastructure dashboard → Dead tuples and Table sizes.
2. `docker compose exec postgres psql -U dmc -d dmc268 -c "select last_autovacuum, n_dead_tup, n_live_tup from pg_stat_user_tables where relname = 'pr_review_jobs'"`.
3. A long-running transaction blocks vacuum:
   `select pid, xact_start, state from pg_stat_activity order by xact_start`.

**Silence:** `alertname=QueueTableBloat` after a manual `VACUUM`, until the next sample.

## Periodic task stale

**Means:** a periodic task (today only the lease reaper, every 30 s) has not succeeded
for two intervals plus 180 s. The `task` label says which one.

**First checks:**
1. Worker logs for `worker.reaper.failed`.
2. The advisory-lock leader: only one worker runs periodic tasks, so check that one
   holds it (worker logs at start).
3. Postgres reachable from the worker (*Target down* for `db`).

**Silence:** `alertname=PeriodicTaskStale` with `task`, while that task is disabled on
purpose.

## No throughput

**Means:** no review was enqueued in 4 hours. It is notified only 13:00–19:00
Europe/Berlin, Mon–Fri, so the 4-hour window lies inside working hours. It backs up the
canary against the silent failure: everything looks healthy and nothing happens.

**First checks:**
1. GitHub App settings → *Advanced* → *Recent deliveries*: are deliveries arriving, and
   with what answer?
2. API dashboard → Webhook outcomes: `unauthorized` points to a webhook secret mismatch
   after a rotation; `rejected` to a payload change.
3. The App is still installed on active repositories.

**Silence:** `alertname=NoThroughput` over a holiday.

## Canary review stale

**Means:** the canary PR (`dmc268-canary`) has had no completed review for 4 h 15 min,
two missed runs. The pipeline is broken end to end: webhook, queue, worker, forge, LLM
or posting. It is notified 13:15–23:15 Europe/Berlin, Mon–Fri.

**First checks:**
1. The canary workflow's runs in `dmc268-canary`: did it push?
2. Pipeline dashboard → Failures by error_kind, and Sentry issues of the last hours.
3. The job of the canary's PR: `select status, error_kind, finished_at from pr_review_jobs where repo_id = <CANARY_REPO_ID> order by created_at desc limit 5`.

**Silence:** `alertname=CanaryReviewStale` while the canary repository is changed.

## Stripe silence

**Means:** no Stripe event in 24 hours while subscriptions are active, so entitlements
may be drifting.

**First checks:**
1. Stripe dashboard → Developers → Webhooks: failed deliveries to our endpoint.
2. API logs for the Stripe webhook route.
3. The webhook signing secret after a rotation.

**Silence:** `alertname=StripeSilence` during a Stripe-side incident.

## Forge rate limit

**Means:** under 10% of the forge's API rate limit was left for 10 minutes. Reviews will
start failing as `forge_unavailable`.

**First checks:**
1. Pipeline dashboard → Forge rate-limit headroom: which provider.
2. A burst of webhooks from one installation (API dashboard → Webhook outcomes).
3. GitHub's rate limit resets hourly; the worker retries `forge_unavailable` with
   backoff.

**Silence:** `alertname=ForgeRateLimit` during a known bulk event.

## Security events

**Means:** over 20 refusals of one kind in 15 minutes: `oauth_state_mismatch`,
`csrf_rejected` or `id_not_found`. These are takeover or enumeration attempts, or a
broken frontend. `session_rejected` is left out.

**First checks:**
1. API dashboard → Security events by kind.
2. Loki, by request: `{service="api"} | json | http_status="403"`. Is it one client or
   many?
3. A frontend deploy just before (a missing `X-Requested-With` makes every request
   `csrf_rejected`).

**Silence:** `alertname=SecurityEvents` with `kind`, while a frontend fix rolls out.

## LLM spend

**Means:** LLM spend in the last 24 hours is over `llm_daily_budget_usd`.

**First checks:**
1. Pipeline dashboard → LLM tokens and cost per model: which model and purpose.
2. A retry loop: Pipeline → Failures by error_kind, with `llm_output_invalid` retries.
3. One installation with many large PRs (Loki, `{service="worker"} | json | event="job.finished"` by `installation_id`).

**Silence:** `alertname=LlmSpend` only with the budget owner's agreement.

## Blind spot

The API availability and webhook ingest SLOs count only requests that reached the API.
While the API, Caddy or the VPS is down, nothing is counted, so *Target down* and *Site
unreachable* report the outage instead. For webhooks the loss is real: GitHub marks a
delivery failed on an error or after 10 s without an answer, and does not retry it. Every
outage of the API or the site therefore ends with the redelivery step under *Target
down*. GitLab deliveries lost in an outage come back with the next push.
