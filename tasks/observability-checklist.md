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

- [ ] **Run Bootstrap** — the Bootstrap workflow (root), so `/opt/dmc268/logs` exists and
      is owned by 10001 before the first deploy with `LOG_DIR`.
- [ ] After the deploy: `api.jsonl` and `worker.jsonl` in `/opt/dmc268/logs`, rotated
      and gzipped after midnight UTC.

## Phase 2: Sentry

- [ ] A deliberate error in production reaches Sentry with release and environment; the
      admin gets the email.
- [ ] After a week: span usage against the quota; lower `traces_sample_rate` if needed.

## Phase 3: VPS and Grafana

- [ ] **Postgres exporter role** — once, in a quiet period: restart Postgres with the
      new `command`, run the migration by hand (`docker compose exec api alembic upgrade
      head`), then create the role in `psql` with the password from the secret:
      `CREATE ROLE dmc268_exporter LOGIN PASSWORD '…'; GRANT pg_monitor TO dmc268_exporter;`.
- [ ] Grafana shows host, container, Postgres and API metrics; Loki finds a line by
      `trace_id`.
- [ ] Active series below 50% of the free limit (usage dashboard).

## Phase 4: Telegram, Sentry relay, Grafana alerting

- [ ] Telegram — bot from @BotFather, a team group with the bot in it, its chat id →
      secrets `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- [ ] Grafana contact point `telegram` with the same token and chat id (placeholders
      until the bot exists).
- [ ] **Rule routing check** — how Prometheus-format rules reach the `telegram` contact
      point (Mimir ruler and its Alertmanager vs Grafana-managed import); decide before
      the rules are uploaded.
- [ ] Notification policy — group by `domain`, 30 s / 5 min / 4 h; a child policy with
      Europe/Berlin mute timings for "No throughput" and "Canary review stale", unless
      `sync.py` provisions them.
- [ ] Sentry internal integration `telegram-relay` — webhook
      `https://<site>/api/internal/alerts/sentry`, Alert Rule Action on; its client
      secret → secret `SENTRY_WEBHOOK_SECRET`.
- [ ] Sentry alert rules — New issue, Regression, Error burst (> 10 in 10 min), Critical
      (`fatal`); each with the email and `telegram-relay` actions.
- [ ] From the first real relay delivery: which payload field is the event timestamp
      (⚠️ in § Alerts).
- [ ] Run the `observability-sync` workflow; dashboards and the synthetic check exist.
- [ ] Drills, in a quiet period: a test error → email + relay; stop api → one "Target
      down"; stop Alloy → "Telemetry missing"; stop Caddy → "Site unreachable". After the
      api and Caddy stops, redeliver GitHub's failed deliveries as the runbook says.
- [ ] A deploy shows as a Grafana annotation and a Sentry release with commits.

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
