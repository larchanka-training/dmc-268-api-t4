# Todo — Observability

Spec: [docs/observability.md](../docs/observability.md); section names below (§ …) are
its sections, and its tables are the exact requirements. Work outside the code (accounts,
secrets, VPS, drills, team decisions): [observability-checklist.md](observability-checklist.md).

Rules for every step:
- AGENTS.md hard rules; 2 and 3 apply to every log line, span, metric label and alert.
- Tests never touch the network: Sentry through a test transport, Grafana and Telegram
  through `httpx.MockTransport`.
- **Do NOT commit anything**: no `git commit`, no push, no PR. Leave every change in the
  working tree.
- **Stop after each phase is coded**: run the five AGENTS.md commands, report the result
  and wait; start the next phase only when told to.
- **Blocked by** names a checklist item. Build and test the code anyway; only the
  production exit waits for it.

## Phase 1: structured logging

- [x] 1. Dependency — `structlog` in `pyproject.toml`; `uv lock`; re-export
      `requirements.txt` (AGENTS.md § Commands).
- [x] 2. Settings — `adapters/observability/config.py`: `ObservabilitySettings.from_env()`
      in the style of `worker/config.py`: `ENV` (default `local`), `LOG_LEVEL` (default
      `INFO`), `LOG_DIR` (unset = stdout only), `IMAGE_TAG` (release); service name from
      the caller.
- [x] 3. Redaction — `domain/redaction.py`, pure: secret patterns (GitHub tokens, `sk-…`,
      bearer headers, PEM blocks, credentials in URLs) → masked text + count per rule
      (BACKEND_ARCHITECTURE.md § Secret redaction). LLM egress use is not in this step.
- [x] 4. Logging config — `adapters/observability/logging.py`:
      `configure_logging(settings, service)`. structlog over stdlib, JSON renderer, the
      § Fields keys, contextvars merge, `ExtraAdder`. Processors: our-logger rule
      (`record.msg`, `record.args` dropped, except the third-party list), allowlist
      (production drops unknown keys; `prompt`/`reply` only in `local`), redaction +
      512-character cut, exception renderer (frames + type of each chained exception;
      no `exc_value`, no locals outside `local`).
- [x] 5. File handler — same module: `TimedRotatingFileHandler`, midnight UTC, 14
      backups, gzip `namer`/`rotator`, `<LOG_DIR>/<service>.jsonl`, only when `LOG_DIR`
      is set.
- [x] 6. Entry points — `configure_logging` replaces `logging.basicConfig` in `main.py`
      and `worker/__main__.py`.
- [x] 7. Access log — middleware in `api/main.py`: `request_id` (read or create, echo as
      `X-Request-ID`), contextvars binding, one JSON line per request (`http.method`,
      `http.route` template, `http.status`, `duration_ms`); `uvicorn.access` silenced; the
      callback query string never logged (replaces `CallbackQueryLogFilter`).
- [x] 8. Retry loops — `worker/listener.py`, `worker/loop.py`, `worker/periodic.py` log
      one `warning` when failures start and one `info` when they clear
      (`tests/worker/test_retry_logging.py`).
- [x] 8a. Call-site audit — every other `logger.*` call in `adapters/`, `api/`, `worker/`
      (~50) becomes a dotted `event` + fields.
- [x] 9. `DEBUG_LOG_DIFF` — `worker/process.py` honours it only when `ENV == local`;
      otherwise ignored, with one warning at startup.
- [x] 10. Lint — add `G` to ruff `select` in `pyproject.toml`; fix the findings.
- [x] 11. Deploy — `deploy/compose.yml`: `./logs:/logs` on api and worker; `deploy.yml`
      writes `ENV=production` and `LOG_DIR=/logs` to `.env`; `ops/bootstrap.sh` creates
      `/opt/dmc268/logs` owned by 10001. Production exit **blocked by:** "Run Bootstrap".
- [x] 12. Tests — `tests/adapters/test_observability_logging.py`: the Phase 1 test list
      of the spec, plus the canary test (marker in diff, PR title and reply; invalid
      JSON, schema mismatch, provider error, all providers failed; marker absent from
      captured output).
- [x] 13. Verify — five commands green; stop.

## Phase 2: Sentry errors and API traces

- [x] 14. Dependency — `sentry-sdk` 2.73.0 (its FastAPI, httpx and SQLAlchemy
      integrations enable themselves); lock; re-export.
- [x] 15. Init — `adapters/observability/sentry.py`: `init_sentry(SentrySettings)`, a no-op
      without `SENTRY_DSN`; no PII (the SDK default), `max_request_body_size`,
      `include_local_variables`, `environment` (`ENV`) and `release` (`IMAGE_TAG`) as in
      § Sampling and privacy (`tests/adapters/test_observability_sentry.py`).
- [x] 15a. Integrations — auto-enabling off; Starlette, FastAPI, httpx and SQLAlchemy
      listed, so no LLM SDK integration can switch itself on.
- [x] 16. Sampler and query scrub — `traces_sampler`: 0 for `/health` and
      `GET /jobs/{job_id}/events`, else 1.0; the query string dropped from events and
      transactions.
- [x] 16a. Scrubbers — `before_send`: redaction, allowlisted headers, no query, body or
      cookies; in production each `exception.values[].value` → the `error_kind`,
      `logentry.formatted`/`params` dropped, `extra` allowlisted. `before_breadcrumb`: in
      production a logging breadcrumb keeps its event name only. (Dropping events from
      the relay route moved to step 34.)
- [x] 17. Wiring — `init_sentry` in `build_app` and `worker/__main__.py`; `deploy.yml`
      writes `ENV=production`.
- [x] 17a. Context — structlog processor for `trace_id`/`span_id`; Sentry tags
      `installation_id`, `job_id`, `delivery_id`, `request_id`, `error_kind` from the same
      contextvars (the worker binds each job, the webhook its delivery).
- [x] 18. Failed reviews — `worker/process.py`: a job that ends `FAILED` logs at `error`
      with `exc_info` (pass the exception into `_record_failure`); `RETRYING` stays
      below `error`.
- [x] 19. Tests — test transport: scrubbers; exactly one event per `FAILED` job, none
      per `RETRYING`; no-op without a DSN; canary test extended to events and
      breadcrumbs.
- [x] 20. Verify — five commands green; stop. Production exit **blocked by:** "Sentry project".

## Phase 3: metrics and Alloy

- [ ] 21. Dependency — `prometheus-client`; lock; re-export.
- [ ] 22. Definitions — `adapters/observability/metrics.py`: every metric of
      § Application with its labels; each counter and histogram initialised at 0 for
      every known label combination; timestamp gauges left absent.
- [ ] 23. Servers — metrics server on `:9100` in the API lifespan and `:9101` in the
      worker; nothing on the API's main port.
- [ ] 24. HTTP metrics — middleware: `http_requests_total` and
      `http_request_duration_seconds` by route template; the SSE route is not observed
      by the histogram.
- [ ] 25. Webhook outcomes — `api/webhooks/github.py`: `webhook_deliveries_total` with
      `enqueued`, `duplicate`, `filtered`, `unauthorized`, `too_large`, `rejected` (400
      after a valid signature), `error` (5xx).
- [ ] 26. Job metrics — worker: `jobs_finished_total{status,error_kind}`,
      `review_duration_seconds{status}` (enqueue → terminal; buckets include
      60/120/300/600/900), `job_claim_latency_seconds` (from `next_attempt_at`),
      `worker_in_flight`. Label values are the database's (`COMPLETED`).
- [ ] 27. DB sampler — API lifespan task, every 15 s: `queue_jobs{status}` and
      `queue_oldest_claimable_age_seconds` as defined in § Application; a failed query
      removes its gauges.
- [ ] 28. Caddy — `deploy/Caddyfile`: global `servers { metrics }` and a `:2020`
      `metrics` site block; `:2020` not published in compose.
- [ ] 29. Postgres — compose `command` with `shared_preload_libraries=pg_stat_statements`;
      `alembic/versions/0003_pg_stat_statements.py` (`CREATE EXTENSION IF NOT EXISTS`);
      both in `docs/db_models_and_migrations.md`. The exporter role is a checklist item.
- [ ] 30. Alloy — compose `alloy` service: pinned image, no ports, read-only
      `/var/run/docker.sock`, `/sys`, `/proc` and `/opt/dmc268/logs`, WAL on a named
      volume, `GRAFANA_CLOUD_*` and `POSTGRES_EXPORTER_PASSWORD` by interpolation only.
      `deploy/alloy/config.alloy`: scrape `api:9100`, `worker:9101`, `caddy:2020` every
      60 s; `unix` (cpu, meminfo, filesystem, diskstats, netdev, loadavg); `cadvisor`
      (`docker_only`, no container labels, relabel allowlist including OOM); `postgres`
      (top 20 statements); tail `/logs/*.jsonl` to Loki; remote write.
- [ ] 31. Deploy — `deploy.yml`: copy the Alloy config; write the new variables to
      `.env`; warn, not fail, when Grafana secrets are missing; the smoke test asserts
      `/api/metrics` is 404; port check unchanged (22/80/443).
- [ ] 32. Tests — expected metric names exposed; route label is the template; main port
      has no `/metrics`; one webhook outcome per branch; SSE absent from the histogram;
      sampler clears gauges on a failed query.
- [ ] 33. Verify — five commands green; stop. Production exit **blocked by:** "Grafana Cloud
      stack", "Postgres exporter role".

## Phase 4: alerts, dashboards, relay

- [ ] 34. Relay — `api/internal_alerts.py`, `POST /internal/alerts/sentry`: `404`
      without `SENTRY_WEBHOOK_SECRET`; `Sentry-Hook-Signature` (HMAC-SHA256) else `401`;
      drops stale (> 30 min), duplicate (issue + event id, 1 h, bounded) and over
      30/min, each logged `alert.relay_dropped` and counted by reason; message = level,
      title (redacted, ≤ 200 chars), env, release, count, link; failures `warning` +
      `alert_relay_failures_total`. Exempt the route from CSRF in `api/deps.py` (server
      to server, like `/webhooks/`); `before_send` drops any event raised inside it.
- [ ] 35. Notifier — `adapters/alerting/telegram.py`: `TelegramNotifier` (`sendMessage`)
      and `NullNotifier` (logs `alert.dropped`) while `TELEGRAM_BOT_TOKEN` is unset;
      settings for `TELEGRAM_*` and `SENTRY_WEBHOOK_SECRET`.
- [ ] 36. Relay tests — valid signature forwards; bad or missing `401`; stale and
      repeated dropped; failing notifier → warning and no Sentry event; notifier against
      `httpx.MockTransport`.
- [ ] 37. Alert rules — `ops/observability/alerts/*.rules.yaml`: every row of § Alerts
      with its exact expression; labels `domain`, `severity`, `owner`; annotation
      `runbook_url`; dependants carry their root's `unless on()`. Upload format
      **blocked by:** "Rule routing check"; the files and tests are not.
- [ ] 38. Rule tests — `ops/observability/alerts/tests/` for `promtool test rules`: per
      rule fire, not fire, absent series, suppressed by its root; a `ci.yml` job runs
      promtool from a pinned Prometheus image.
- [ ] 39. Dashboards — `ops/observability/dashboards/{overview,api,pipeline,infrastructure}.json`
      with the § Dashboards panels.
- [ ] 40. Synthetics — `ops/observability/synthetics/uptime.json`: HTTP
      `https://<site>/api/health`, every 2 min, 3 EU probes, TLS expiry.
- [ ] 41. Sync — `ops/observability/sync.py`: idempotent push of dashboards, rules and
      synthetics through the Grafana APIs (`GRAFANA_SA_TOKEN`); tests with
      `httpx.MockTransport`; `.github/workflows/observability-sync.yml`
      (`workflow_dispatch`).
- [ ] 42. Deploy markers — `deploy.yml` after the smoke test: Grafana annotation (tag
      `deploy`, text `IMAGE_TAG`); `sentry-cli releases new`, `set-commits --auto`,
      `deploys new`, with `fetch-depth: 0` on that job's checkout; each skipped with a
      warning when its token is missing.
- [ ] 43. Runbooks — `docs/runbooks.md`: one section per alert (meaning, first three
      checks, how to silence); "Target down" (api) and "Site unreachable" end with the
      redelivery step of § SLOs, "Blind spot".
- [ ] 44. Verify — five commands green; stop. The drills are checklist items.

## Phase 5: pipeline instrumentation

- [ ] 45. Classification — move `_classify` from `worker/process.py` to
      `domain/errors.py` as a public function; behaviour unchanged; tests follow it.
- [ ] 46. Observer — `Observer` port in `domain/ports.py` (async `step()` context
      manager, `StepHandle.set`); `adapters/observability/observer.py` wrapping
      `StepRecorder`: row, span, `review_step_duration_seconds`, `step.finished`; an
      exception ends the step `FAILED` with its `error_kind`; a failed row write is
      logged and ignored. Recording fake in `tests/`; it replaces `_Steps` in
      `worker/process.py`.
- [ ] 47. Job trace — migration adding `pr_review_jobs.trace_context` (+
      `docs/db_models_and_migrations.md`); store `sentry-trace` and `baggage` at
      enqueue; continue as the `review.job` root span on claim.
- [ ] 48. LLM — `adapters/llm/`: spans `llm.rule_screen`, `llm.attempt` (purpose
      `review`/`correction`), `llm.verify`; `llm_requests_total`, `llm_tokens_total`,
      `llm_key_rotations_total` with `purpose`. `llm_cost_usd_total` and the API's
      `llm_daily_budget_usd` gauge **blocked by:** "LLM prices and budget".
- [ ] 49. Forge — `forge_rate_limit_remaining_ratio` from `X-RateLimit-*` in
      `adapters/github/client.py` and `publisher.py` (lowest in the last 5 min).
- [ ] 50. Security events — `security_events_total`: `oauth_state_mismatch`
      (`api/auth.py`), `csrf_rejected` (`enforce_csrf`), `session_rejected`
      (`current_session`), `id_not_found` (the `ApiError` handler, 404 on a route with a
      path id).
- [ ] 51. Reaper — `lease_expiries_total` from `reap_expired()`'s count; migration for
      `periodic_task_runs (task, last_success_at)` (+ db_models doc), upserted by the
      leader after each tick; the DB sampler exports
      `periodic_task_last_success_timestamp` and `periodic_task_interval_seconds`.
- [ ] 52. Drill rule — `ops/observability/alerts/drill/no_throughput_drill.rules.yaml`
      (15 min lookback, no time window), outside the normal sync; used once by the
      checklist drill, then deleted.
- [ ] 53. Tests and verify — the fake sees row, span, metric and log per step; LLM
      metrics per purpose; one security kind per refusal path; five commands green; stop.

## Phase 6: canary and SLOs

- [ ] 54. Canary gauge — DB sampler: `canary_review_last_success_timestamp` = newest
      `finished_at` of a `COMPLETED` job for `CANARY_REPO_ID`; no gauge when unset.
- [ ] 55. Canary workflow — `ops/observability/canary/canary.yml`: the scheduled
      workflow for `dmc268-canary` (a known-defect commit pushed to an open PR at
      09–19 Europe/Berlin, Mon–Fri); copied there by hand (checklist).
- [ ] 56. SLOs — 28-day recording rules and fast/slow burn alerts for the four SLOs of
      § SLOs, with the ≥ 3 bad events guard and root suppression; `promtool` tests;
      error-budget panel on Overview.
- [ ] 57. Verify — five commands green; stop.

## Docs, in the phase that needs them

- [ ] 58. `docs/devops.md` secrets table: each new name of § Configuration and secrets.
- [ ] 59. `docs/configuration.md`: `llm_prices`, `llm_daily_budget_usd` (phase 5).
      **Blocked by:** "LLM prices and budget".
- [ ] 60. `AGENTS.md` open question 4 marked resolved, and the two ⚠️ about it removed
      from the spec. **Blocked by:** "Secret storage".
- [ ] 61. `AGENTS.md` layout table: `adapters/observability/`, `adapters/alerting/`,
      `ops/observability/`.
