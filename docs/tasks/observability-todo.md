# Todo — Observability

Spec: [docs/observability.md](../observability.md); section names below (§ …) are
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

- [x] 21. Dependency — `prometheus-client`; lock; re-export.
- [x] 22. Definitions — `adapters/observability/metrics.py`: the metrics of § Application
      that this phase records, with their labels, in one registry per process; each
      counter and histogram initialised at 0 for every known label combination; the
      sampled gauges absent until a sample succeeds. Every later metric lands with its
      instrumentation (phases 4–6): a counter exported at 0 before anything increments it
      reads as "nothing happened".
- [x] 23. Servers — metrics server on `:9100` in the API lifespan and `:9101` in the
      worker; nothing on the API's main port.
- [x] 24. HTTP metrics — recorded by the access log middleware: `http_requests_total` and
      `http_request_duration_seconds` by route template; the SSE route is not observed
      by the histogram.
- [x] 25. Webhook outcomes — `api/webhooks/github.py`: `webhook_deliveries_total` with
      `enqueued`, `duplicate`, `filtered`, `unauthorized`, `too_large`, `rejected` (400
      after a valid signature), `error` (5xx).
- [x] 26. Job metrics — worker: `jobs_finished_total{status,error_kind}`,
      `review_duration_seconds{status}` (enqueue → terminal; buckets include
      60/120/300/600/900), `job_claim_latency_seconds` (from `next_attempt_at`),
      `worker_in_flight`. Label values are the database's (`COMPLETED`). `ClaimedJob`
      carries the store's enqueue, claimable and claim times, so no two clocks are
      compared.
- [x] 27. DB sampler — API lifespan task, every 15 s: `queue_jobs{status}` and
      `queue_oldest_claimable_age_seconds` as defined in § Application; a failed query
      removes its gauges. Both job stores implement `queue_snapshot()`.
- [x] 28. Caddy — `deploy/Caddyfile`: the global `metrics` option (Caddy 2.11 deprecates
      `servers { metrics }`) and a `:2020` `metrics` site block; `:2020` not published.
- [x] 29. Postgres — compose `command` with `shared_preload_libraries=pg_stat_statements`;
      `alembic/versions/0003_pg_stat_statements.py` (`CREATE EXTENSION IF NOT EXISTS`);
      both in `docs/db_models_and_migrations.md`. The exporter role is a checklist item.
- [x] 30. Alloy — compose `alloy` service (profile `monitoring`): pinned image, no ports,
      usage reporting off (`--disable-reporting`), read-only Docker and containerd
      sockets, `/sys`, `/proc` and `./logs`, WAL on a named volume, `GRAFANA_CLOUD_*` and
      `POSTGRES_EXPORTER_PASSWORD` by interpolation only. `deploy/alloy/config.alloy`: scrape `api:9100`, `worker:9101`, `caddy:2020`
      every 60 s; `unix` (cpu, meminfo, loadavg, filesystem, diskstats, and vmstat's
      `oom_kill` for the OOM alert; no netdev, which sees only Alloy's network
      namespace); `cadvisor` (`docker_only`, no container labels, relabel allowlist of
      CPU, memory, restarts and its `up`); `postgres` (top 20 statements); tail
      `/logs/*.jsonl` to Loki; remote write. Checked end to end by the lab
      (`uv run deploy/lab/lab.py test`): every target up, both pushes arrive.
- [x] 31. Deploy — `deploy.yml`: copy the Alloy config; write the new variables and
      `COMPOSE_PROFILES=monitoring` to `.env` only when all six exist, else warn; the
      smoke test asserts `/api/metrics` is 404; port check unchanged (22/80/443).
      `ci.yml` validates compose with the profile, and the Caddyfile and Alloy config
      with the images compose runs.
- [x] 32. Tests — expected metric names exposed; route label is the template; main port
      has no `/metrics`; one webhook outcome per branch; SSE absent from the histogram;
      sampler clears gauges on a failed query; claim times and queue snapshot in the
      job store conformance suite.
- [x] 33. Verify — five commands green; stop. Production exit **blocked by:** "Grafana Cloud
      stack", "Postgres exporter role".

## Phase 4: alerts, dashboards, relay

- [x] 34. Relay — `api/internal_alerts.py`, `POST /internal/alerts/sentry`: `404`
      without `SENTRY_WEBHOOK_SECRET`; `Sentry-Hook-Signature` (HMAC-SHA256 of the
      received bytes) else `401`; `413` over 1 MB; a signed delivery that is not
      `event_alert` gets `204` and `alert.relay_ignored`. `domain/alerts.py`
      (`RelayGate`) drops stale (> 30 min, from `data.event.timestamp`, else `datetime`),
      duplicate (issue + event id, 1 h, bounded) and over 30/min, each logged
      `alert.relay_dropped` and counted in `alert_relay_dropped_total{reason}`. Answers
      202 and forwards after the response (Sentry allows one second). Message: level,
      rule, environment, release, title (redacted, ≤ 200 chars), link; Sentry's payload
      has no event count, so the rule name stands in. Failures `warning` +
      `alert_relay_failures_total`. CSRF-exempt in `api/deps.py`; `before_send` drops any
      event from the route and `traces_sampler` skips it. The Bot API's URL holds the
      token and httpx logs it: new `telegram_bot_token` redaction rule (ruleset 2).
- [x] 35. Notifier — `adapters/alerting/telegram.py`: `TelegramNotifier` (`sendMessage`,
      plain text) and `NullNotifier` (logs `alert.dropped`) while `ALERTS_TELEGRAM_BOT_TOKEN`
      or `ALERTS_TELEGRAM_CHAT_ID` is unset; `AlertNotifier` port in `domain/ports.py`;
      `adapters/alerting/config.py` for `ALERTS_TELEGRAM_*` and `SENTRY_WEBHOOK_SECRET`, which
      `deploy.yml` writes to `.env` when set.
- [x] 36. Relay tests — valid signature forwards; bad or missing `401`; stale (also from
      `datetime`), repeated and over-limit dropped and counted; failing notifier →
      warning, counter and no Sentry event or transaction; a crash in the route reaches
      no Sentry event; the bot token reaches no log line (fails without the redaction
      rule); notifier against `httpx.MockTransport`.
- [x] 37. Alert rules — `ops/observability/alerts/{api,worker,db,edge,host,monitoring,
      business}.rules.yaml`: 24 rules for the 22 rows of § Alerts ("Target down" is
      three rules, one per process and domain; the SLO burn alerts are step 56). Labels
      `domain` (the file), `severity`, `owner` (`unassigned` until the checklist's
      "Owners"); annotations `summary`, `runbook_url`; dependants carry their root's
      `unless on()`. "API error rate" and "API latency" leave `/health` out. Upload:
      Grafana-managed rules through the provisioning API, in the form Grafana's own
      import produces (the import API itself is 404 on Grafana Cloud); verified against
      Grafana 13.2 in the lab; the Grafana Cloud check stays **blocked by:** "Rule
      routing check".
- [x] 38. Rule tests — `ops/observability/alerts/tests/*.test.yaml` for `promtool test
      rules`: per rule fire, not fire, absent series, suppressed by its root; `ci.yml`
      job `alert-rules` runs promtool from the lab's pinned Prometheus image.
      `tests/ops/test_observability_rules.py` holds the files to the spec's table, the
      labels and the runbook anchors.
- [x] 39. Dashboards — `ops/observability/dashboards/{overview,api,pipeline,infrastructure}.json`,
      generated by `dashboards/build.py` from a compact panel list (a test fails when
      the JSON is stale); a `prometheus` datasource variable, deploy annotations, the
      Sentry traces link on API latency (`sentry_url`, set by `SENTRY_ORG_URL`). The SLO
      budget panel is step 56.
- [x] 40. Synthetics — `ops/observability/synthetics/uptime.json`: HTTP
      `https://<site>/api/health`, every 2 min, Frankfurt, London and Paris (names, so
      `sync.py` resolves the ids), basic metrics, job `dmc268-site`.
- [x] 41. Sync — `ops/observability/sync.py`: idempotent push of the dashboards (folder
      DMC-268), rules (provisioning API, folder "DMC-268 alerts", removed groups
      deleted), alerting (contact point
      `telegram` created once with placeholders and never overwritten, the two mute
      timings, the policy grouped by `domain`) and the synthetic check (Synthetic
      Monitoring API: `GRAFANA_SM_URL`, `GRAFANA_SM_TOKEN`); `--only`, `--dry-run`; tests
      with `httpx.MockTransport`; `.github/workflows/observability-sync.yml`
      (`workflow_dispatch`). The lab's `up` runs it against its own Grafana.
- [x] 42. Deploy markers — `deploy.yml` after the last check: Grafana annotation (tag
      `deploy`, text `IMAGE_TAG`) with `GRAFANA_URL` and `GRAFANA_SA_TOKEN`; `@sentry/cli`
      3.8.2 `releases new`, `set-commits --auto --ignore-missing`, `finalize`, `deploys
      new -e production`, with `fetch-depth: 0` on the job's checkout; each skipped with
      a warning when its token is missing, and neither fails the deploy.
- [x] 43. Runbooks — `docs/runbooks.md`: one section per alert (meaning, first three
      checks, how to silence); "Target down" and "Site unreachable" end with the
      redelivery step of § SLOs, "Blind spot".
- [x] 44. Verify — five commands green; promtool green; the lab's `test` passes (34
      checks), and `drill worker-down --alert` sees Grafana fire Target down. The drills
      in production are checklist items.

## Phase 5: pipeline instrumentation

- [x] 45. Classification — `classify` and `ERROR_KINDS` in `domain/errors.py`, public;
      behaviour unchanged; `tests/domain/test_error_kinds.py`.
- [x] 46. Observer — `Observer` and `StepHandle` ports in `domain/ports.py`;
      `adapters/observability/observer.py` (`StepObserver`) wraps `StepRecorder`: the
      row, a `review.step` span named after the step (its attributes: counts, sizes,
      provider, model), `review_step_duration_seconds{step, outcome}` and
      `step.finished`; an exception ends the step `FAILED` with its `error_kind`; a
      failed row write is logged and ignored. It replaces `_Steps`; `process_claimed_job`
      takes an `observer` (the tests' recording fake). The four recorded steps' series
      start at 0.
- [x] 47. Job trace — migration 0004 adds `pr_review_jobs.trace_context` (JSONB; +
      `docs/db_models_and_migrations.md`); the webhook stores `sentry-trace` and
      `baggage` at enqueue (none without a DSN); each job runs in a `review.job`
      transaction continuing it, so the worker's log lines carry the webhook's trace id.
- [x] 48. LLM — spans `llm.attempt` (one per HTTP request, under `llm_call`),
      `llm.rule_screen`, `llm.verify`, with counts, names and outcomes only;
      `llm_requests_total`, `llm_tokens_total`, `llm_key_rotations_total` with `purpose`
      (`review`, `correction`, `summary`, `rule_screen`, `verify`), started at 0 by the
      factory; TypeSafe as `provider="typesafe"`. `llm-smoke.yml` sends its spans with a
      DSN (`ENV=smoke`). `llm_cost_usd_total` and the API's `llm_daily_budget_usd` gauge
      **blocked by:** "LLM prices and budget".
- [x] 49. Forge — `forge_rate_limit_remaining_ratio` from an httpx response hook on
      every App and installation client in `adapters/github/` (`rate_limit.py`; a test
      fails on a client without it), lowest in the last 5 min, `core` only, in both
      processes.
- [x] 50. Security events — `security_events_total`: `oauth_state_mismatch`
      (`api/auth.py`), `csrf_rejected` (`enforce_csrf`, both refusals), `session_rejected`
      (`current_session`, a cookie we do not know or one expired; no cookie is not one),
      `id_not_found` (the `ApiError` handler, 404 on a route with a path id).
- [x] 51. Reaper — `lease_expiries_total` from each leader tick's count; migration 0005
      for `periodic_task_runs (task, last_success_at, interval_seconds)` (+ db_models
      doc), upserted by the leader after each tick; the DB sampler exports
      `periodic_task_last_success_timestamp` and `periodic_task_interval_seconds`, read
      separately from the queue so one failing leaves the other.
- [x] 52. Drill rule — `ops/observability/alerts/drill/no_throughput_drill.rules.yaml`
      (15 min lookback, no time window) with a promtool test in CI; pushed with `sync
      --only drill`, removed by the next `rules` sync.
- [x] 53. Tests and verify — the observer's row, span, metric and log per step; the
      trace from webhook to review; LLM metrics and spans per purpose with no prompt or
      reply text; one security kind per refusal path; the rate-limit hook on every
      client; periodic runs on memory and Postgres; five commands green.

## Phase 5b: the task's definition of done

The gaps a check against the task's DoD found (spec § Definition of done, items 7–9).

- [x] 62. LLM latency — `llm_request_duration_seconds{provider, model, purpose}`, observed
      for each request that got a reply (OpenAI-compatible and TypeSafe), started at 0 by
      the factory; "LLM latency p95" on Overview (with queue depth and RPS) and p50/p95
      by provider on Pipeline.
- [x] 63. Context — `BUILD_CONTEXT` recorded on its own: parsing, filtering and the job
      stats leave `FETCH_DIFF` (a diff that does not parse fails `BUILD_CONTEXT`);
      attributes `files`, `reviewable`, `skipped`, `chunks`.
- [x] 64. DB — `worker_db_duration_seconds{operation}` around the worker's Postgres calls
      (claim, lease, posted fingerprints, findings, complete, fail, get); "Worker DB calls
      p95 by operation" on Pipeline.
- [x] 65. `findings_posted_total{severity}` from what the forge accepted, started at 0.
- [x] 66. Container logs — `config.alloy` ships this compose project's `caddy` and
      `postgres` logs from Docker (`COMPOSE_PROJECT` in both compose files), levels in the
      app's words, Caddy's query strings cut; four lab checks (49 in all). No dashboard.
- [ ] 67. Postgres backup. **Blocked by:** where the off-server copy goes (spec § Open
      questions, item 4; the team).

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

- [x] 58. `docs/devops.md` secrets table: each new name of § Configuration and secrets.
- [ ] 59. `docs/configuration.md`: `llm_prices`, `llm_daily_budget_usd` (phase 5).
      **Blocked by:** "LLM prices and budget".
- [ ] 60. `AGENTS.md` open question 4 marked resolved, and the two ⚠️ about it removed
      from the spec. **Blocked by:** "Secret storage".
- [x] 61. `AGENTS.md` layout table: `adapters/observability/`, `adapters/alerting/`,
      `ops/observability/`.
