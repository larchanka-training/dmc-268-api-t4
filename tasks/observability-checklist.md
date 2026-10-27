# Checklist — Observability, outside the code

Spec: [docs/observability.md](../docs/observability.md). Code steps:
[observability-todo.md](observability-todo.md); its **Blocked by** notes name the bold
items below. Secrets go into GitHub Secrets only, never into a file or a chat.

## Team decisions

- [ ] **Secret storage** — confirm "environment variables on the VPS" (2026-10-12), so
      AGENTS.md open question 4 can be closed (todo 60).
- [ ] **LLM prices and budget** — where `llm_prices` and `llm_daily_budget_usd` live
      (database config or env) and the daily budget in USD (todo 48, 59).
- [ ] Owners — a person per alert `domain` (`edge`, `api`, `worker`, `db`, `host`,
      `monitoring`, `business`) for the `owner` label.
- [ ] Lease vs LLM time — renew the lease between LLM attempts, or raise it to 900 s
      (worker code, not observability).
- [ ] `502 forge_unavailable` — keep it in the API availability SLO, or exclude it.
- [ ] Sign-in audit — accept `user_id` going to Loki (EU region).
- [ ] Spec open questions 1 (per-account dashboards) and 2 (automatic redelivery).
- [ ] WORKFLOW_DESIGN.md §4's claim that GitHub retries failed deliveries (comment at
      the end of the spec).

## Phase 0: accounts

Sentry:
- [x] **Sentry project** — organisation in the EU region, free plan; the one seat is the
      app admin's email. Projects `dmc268-backend` and `dmc268-frontend`; DSN → secret
      `SENTRY_DSN`.
- [x] Note the plan's error and span quotas in the spec (it assumes ~5k errors a month).
- [x] No GitHub integration.
- [x] Organization Token (fixed scope `org:ci`; Settings → Developer Settings →
      Organization Tokens) → secret `SENTRY_AUTH_TOKEN`. Checked 2026-10-24: `releases
      new`, `set-commits --auto` and `deploys new` all work with it.

Grafana Cloud:
- [x] **Grafana Cloud stack** — free stack in an EU region. Access policy token with
      metrics write and logs write → secret `GRAFANA_CLOUD_TOKEN`; Prometheus and Loki
      URLs and user ids → variables `GRAFANA_CLOUD_PROM_URL`, `GRAFANA_CLOUD_PROM_USER`,
      `GRAFANA_CLOUD_LOKI_URL`, `GRAFANA_CLOUD_LOKI_USER`.
- [x] Note the current limits in the spec (10k series, 14-day retention, 100k
      Synthetic Monitoring executions, 3 active users).
- [x] Service account with dashboard, annotation and alerting write → secret
      `GRAFANA_SA_TOKEN`.
- [x] Synthetic Monitoring enabled in the stack; check whether its API needs a token of
      its own for `sync.py`.

Other:
- [x] Generate `POSTGRES_EXPORTER_PASSWORD` → secret.

## Phase 1: VPS

- [x] **Run Bootstrap** — the Bootstrap workflow (root), so `/opt/dmc268/logs` exists and
      is owned by 10001 before the first deploy with `LOG_DIR`.
- [ ] After the deploy: `api.jsonl` and `worker.jsonl` in `/opt/dmc268/logs`, rotated
      and gzipped after midnight UTC.

## Phase 2: Sentry

- [ ] A deliberate error in production reaches Sentry with release and environment; the
      admin gets the email.
- [ ] After a week: span usage against the quota; lower `traces_sample_rate` if needed.

## Phase 3: VPS and Grafana

### A. Merge and finish phase 3

- [ ] Commit phases 3 and 4 with `tasks/commit-message.txt` (`git add -A && git reset
      tasks/commit-message.txt`, then `git commit -F tasks/commit-message.txt` and delete
      the file); open the PR; CI is green, the new `alert-rules` job included.
- [x] **Postgres exporter role** — `dmc268_exporter` with `pg_monitor`, created by hand
      with the password from the secret (2026-10-27).
- [ ] Merge in a quiet period: the first deploy restarts Postgres with the new `command`
      (a few seconds). Then, once, migration 0003:
      `docker compose exec api alembic upgrade head`.
- [ ] The deploy has no "Alloy not started" warning, and `docker compose ps` lists
      `alloy` as running.
- [ ] Grafana → Explore: `up` is 1 for all 7 targets (alloy, api, cadvisor, caddy, host,
      postgres, worker); host, container, Postgres and API metrics show; Loki finds a
      line by `trace_id`.
- [ ] Active series below 50% of the free limit (usage dashboard).

## Phase 4: Telegram, Sentry relay, Grafana alerting

### B. Telegram bot

- [ ] In Telegram, @BotFather → `/newbot`: a name, and a username ending in `bot`. It
      replies with the token.
- [ ] A team group with the bot in it; send `/start@<botname>` in the group.
- [ ] The group's chat id (a negative number), the token kept out of shell history:
      `read -rs TOKEN`, then
      `curl -s "https://api.telegram.org/bot$TOKEN/getUpdates" | python3 -m json.tool | grep -A3 '"chat"'`,
      then `unset TOKEN`.
- [ ] Secrets `ALERTS_TELEGRAM_BOT_TOKEN` and `ALERTS_TELEGRAM_CHAT_ID`.

### C. Sentry

- [ ] Sentry internal integration `telegram-relay` — Settings → Developer Settings →
      Custom Integrations → Create New Integration → Internal Integration; webhook
      `https://<site>/api/internal/alerts/sentry`, Alert Rule Action on, no permissions.
      Its **Client Secret** (it signs each delivery; not the integration's token) →
      secret `SENTRY_WEBHOOK_SECRET`.
- [ ] Redeploy (Actions → Deploy → Run workflow, or the next merge) once
      `SENTRY_WEBHOOK_SECRET` and the two Telegram secrets exist: the VPS `.env` gets them
      only on a deploy. Until then the relay route answers 404.
- [ ] Sentry alert rules — Monitors → Alerts → Create Alert, project `dmc268-backend`,
      environment `production`; each with "Notify on preferred channel" (the admin,
      email) and "Send a notification via an integration" → `telegram-relay`:
      New issue ("A new issue is created"); Regression ("A resolved issue regresses");
      Error burst ("An event or issue activity is captured" + Number of events more than
      10 in 10 minutes); Critical (same trigger + Event level equals `fatal`). If
      `telegram-relay` is not offered, check that Alert Rule Action is on. The Grafana
      rules are not created by hand: the `observability-sync` workflow pushes them.
- [ ] "Send Test Notification" on one of the rules: a message arrives in the Telegram
      group, and Loki shows `alert.relay_forwarded`
      (`{service="api"} | json | event=~"alert.*"`).
- [ ] From the first real relay delivery: it carries `data.event.timestamp` or
      `datetime`, as the relay reads them (⚠️ in § Alerts). A fresh alert that logs
      `alert.relay_dropped` with `reason=stale` means neither field means what Sentry
      documents.

### D. Grafana

- [ ] Variables `GRAFANA_URL` (`https://<stack>.grafana.net`) and `GRAFANA_SM_URL`
      (Synthetic Monitoring's API URL for the stack's region), secret `GRAFANA_SM_TOKEN`
      (Testing & synthetics → Synthetics → Config → access token). Optional: variables
      `SENTRY_ORG_URL` (for the dashboard's traces link) and
      `GRAFANA_PROM_DATASOURCE_UID` (only if the stack's Prometheus is not
      `grafanacloud-prom`: Connections → Data sources).
- [ ] Actions → Observability sync → Run workflow: with `dry_run` first, then for real.
      A 403 from an `/api/v1/provisioning` call means the service account behind
      `GRAFANA_SA_TOKEN` needs the Admin role.
- [ ] **Rule routing check** — the code uses Grafana's import of Prometheus-format rules
      (Grafana-managed rules, routed by the notification policy); verified on Grafana
      13.2 in the lab. In Grafana Cloud, after the sync: Alerting → Alert rules lists 24
      rules in the folder "DMC-268 alerts", and a rule's notification preview shows the
      `telegram` contact point.
- [ ] Notification policy — `sync.py` provisions it (group by `domain`, 30 s / 5 min / 4 h,
      child routes muting "No throughput" and "Canary review stale" outside working
      hours, Europe/Berlin); check it in Alerting → Notification policies.
- [ ] Grafana contact point `telegram` — `sync.py` creates it with placeholders and never
      overwrites it: Alerting → Contact points → telegram → Edit, type in the bot token
      and chat id, then Test; the message arrives in the group.
- [ ] Dashboards → DMC-268 has the four dashboards; Synthetics has the check
      `dmc268-site`. After a few runs of the check,
      `probe_ssl_earliest_cert_expiry{job="dmc268-site"}` has data (the check sends basic
      metrics only; if it is missing, turn `basicMetricsOnly` off in
      `ops/observability/synthetics/uptime.json`).

### E. Drills and owners

- [ ] Drills, in a quiet period: a test error → email + relay; stop api → one "Target
      down"; stop Alloy → "Telemetry missing"; stop Caddy → "Site unreachable". After the
      api and Caddy stops, redeliver GitHub's failed deliveries as the runbook says.
- [ ] A deploy shows as a Grafana annotation and a Sentry release with commits.
- [ ] Owners — once decided (Team decisions), replace `owner: unassigned` in
      `ops/observability/alerts/*.rules.yaml` and run the sync again.

## Phase 5: pipeline

- [ ] Throughput drill — deploy the drill rule, watch it fire in a quiet period, delete
      it.
- [ ] An `llm-smoke.yml` run with a DSN shows the LLM spans.

## Phase 6: canary

- [ ] Repository `dmc268-canary` — App installed, a long-lived open PR, the workflow
      copied from `ops/observability/canary/canary.yml`. Check whether GitHub's
      `schedule` takes a time zone; if not, the UTC cron shifts by an hour twice a year.
- [ ] Variable `CANARY_REPO_ID`.
- [ ] Canary account on an internal plan, once billing exists.
- [ ] Drill — suspend the App installation on the canary repo; "Canary review stale"
      fires; unsuspend.

## After a month of data

- [ ] Review the SLO targets; promote the fast burn alerts to critical; remove "API
      error rate".
