# AGENTS.md

Backend of an AI pull-request reviewer (team 4, DMC-268): a GitHub App that reviews PRs
with an LLM and posts findings back. Python, FastAPI. Rules below are taken from the
repository docs; each cites its source. Do not add rules without a source.

## Layout

Target layout: [docs/BACKEND_ARCHITECTURE.md § Planned code layout](docs/BACKEND_ARCHITECTURE.md).

| Path | Status | Contents |
|---|---|---|
| `docs/`, `SYSTEM_DESIGN.md`, `main.py` | exists | design documents, FastAPI stub |
| `domain/` | exists | models, ports (`domain/ports.py`), errors; pure Python |
| `adapters/` | exists (`llm/`, `github/`, `identity/`, `memory/`, `jobs/`, `db/`, `observability/`, `alerting/`) | `observability/` sets up JSON logging, Sentry and the Prometheus metrics with the API's DB sampler, and the worker's step `Observer` (docs/observability.md); `alerting/` sends the Sentry relay's alerts to Telegram (or logs them while no bot is set); `jobs/` holds the in-memory `JobRepository`; `db/` holds config/models/session/repository (`PostgresJobStore`); `github/publisher.py` posts reviews; `github/contents.py` reads a repository file (rules.md preview), finding context); `github/compare.py` reads a base...head diff for a finding's context; `db/job_events.py` records pipeline steps and reads them back for the SSE stream (`memory/job_events.py` is its in-memory twin for tests); `db/review_comments.py` keeps published findings (`memory/review_comments.py` is its twin); `gitlab/`, `stripe/`, `vault/` planned; tenancy/billing/auth/config tables still planned |
| `tests/` | exists | pytest suite |
| `api/` | exists | auth, `/repositories`, `/webhooks/github`, `/jobs`, `/jobs/{id}`, `/jobs/{id}/rules`, `/jobs/{id}/findings/{fid}/context`, `/jobs/{id}/file-lines` and the job events stream (`api/jobs.py`), the Sentry → Telegram relay `/internal/alerts/sentry` (`api/internal_alerts.py`), the access log (`api/access_log.py`), app factory; `/v1` routes planned |
| `worker/` | exists (`process.py` processes one job; `loop.py` claim/LISTEN/poll, `run_worker`; `listener.py` LISTEN; `periodic.py` advisory-lock leader + lease reaper; `config.py`, `__main__.py`) | remaining periodic tasks planned: usage rollover, credential health, retention purge, grace expiry |
| `ops/` | exists (`bootstrap.sh`, `publish_smoke.py`, `observability/`) | `observability/`: Grafana dashboards (`dashboards/build.py` generates the JSON), alert rules with `promtool` tests, the synthetic check and `sync.py`, which pushes them (docs/observability.md, docs/runbooks.md); `replay.py` planned |
| `evals/`, `test-prs-dataset/` | exists | LLM eval harness (`python -m evals validate\|run`) and the 29-case gold dataset; see [test-prs-dataset/README.md](test-prs-dataset/README.md) |
| `alembic/` | exists (`0001` review jobs + steps, `0002` published findings, `0003` pg_stat_statements, `0004` jobs' trace_context, `0005` periodic_task_runs) | migrations; tenancy/billing tables later |

## Commands

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy domain adapters
```

`uv.lock` is committed and pins the whole tree. Without uv the pip path still works from
`pyproject.toml` (`pip install -e ".[dev]"`), so uv is not a prerequisite for contributing.
`requirements.txt` is generated from the lock (`uv export --no-dev --no-hashes
--no-emit-project -o requirements.txt`) and is runtime-only; edit `pyproject.toml` and
re-export rather than editing it by hand.

## Hard rules

1. `domain/` imports nothing from `adapters/`, `api/`, `worker/`, `ops/`, or any web or
   database framework, and does no I/O
   ([docs/BACKEND_ARCHITECTURE.md § Planned code layout](docs/BACKEND_ARCHITECTURE.md)).
2. Secrets never go into the repo, an image, logs, API responses, exceptions or the prompt; keys only masked (`sk-…abcd`)
   ([docs/WORKFLOW_DESIGN.md §8](docs/WORKFLOW_DESIGN.md);
   [docs/BACKEND_ARCHITECTURE.md § Security model](docs/BACKEND_ARCHITECTURE.md);
   [docs/llm-gateway.md](docs/llm-gateway.md), PR #8).
3. Raw diffs, prompts and model responses are neither stored nor logged
   ([docs/BACKEND_ARCHITECTURE.md § Secret redaction](docs/BACKEND_ARCHITECTURE.md);
   [docs/llm-gateway.md § Логирование](docs/llm-gateway.md), PR #8).
4. Tests never touch the network: adapters are tested against fixtures or
   `httpx.MockTransport` ([docs/BACKEND_ARCHITECTURE.md § Testing strategy](docs/BACKEND_ARCHITECTURE.md);
   [docs/specs/llm-gateway.md](docs/specs/llm-gateway.md), PR #8).
5. LLM output is always parsed into a schema; anything unparseable is dropped, never posted
   ([docs/BACKEND_ARCHITECTURE.md § Prompt injection](docs/BACKEND_ARCHITECTURE.md);
   [SYSTEM_DESIGN.md §5.6](SYSTEM_DESIGN.md)).
6. Diff content, PR title and description are untrusted data: delimited and labelled in
   the prompt, never concatenated into instructions; model output is only ever comment
   text, never an action ([docs/BACKEND_ARCHITECTURE.md § Prompt injection](docs/BACKEND_ARCHITECTURE.md)).
7. Before a commit, all five commands above are green ([docs/specs/llm-gateway.md](docs/specs/llm-gateway.md), PR #8).
8. Where `docs/BACKEND_ARCHITECTURE.md` and `docs/WORKFLOW_DESIGN.md` disagree on
   behaviour, the workflow document wins ([docs/BACKEND_ARCHITECTURE.md](docs/BACKEND_ARCHITECTURE.md), last line).
9. A log call is a dotted event name with its values in `extra`, never format arguments or
   an f-string. A new field goes on `ALLOWED_FIELDS` in `adapters/observability/logging.py`,
   a new event into the Events table. A field that can hold content (diff, prompt, model
   reply, PR title or description, file content, a request or response body, a token) never
   goes on the allowlist: log a count or a length instead
   ([docs/observability.md § Events, § What is never logged](docs/observability.md);
   `tests/adapters/test_observability_logging.py` checks every call).

## Where to read more

- [.agents/rules/backend.md](.agents/rules/backend.md): layers, adapters, config, tests, migrations.
- [.agents/skills/code-review/SKILL.md](.agents/skills/code-review/SKILL.md): reviewing a PR or local diff.
- [docs/](docs/) and [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md): the design itself.

## Open questions

Documents disagree here. Do not pick a side in code; ask the team.
1. **Queue (resolved):** PostgreSQL, as in [docs/WORKFLOW_DESIGN.md §1](docs/WORKFLOW_DESIGN.md)
   (team decision, 2026-10-04).
2. **LLM providers:** OpenAI and Anthropic ([SYSTEM_DESIGN.md §5.4](SYSTEM_DESIGN.md)) vs
   Eurouter + Ollama behind one OpenAI-compatible adapter ([docs/llm-gateway.md](docs/llm-gateway.md), PR #8).
3. **Severity (resolved):** severity AND category are persisted on published findings
   ([docs/PIPELINE_SPEC.md §7.1, D4](docs/PIPELINE_SPEC.md); decision 2026-10-10, this PR);
   the columns land with the pr_review_comments migration, which is deliberately not `0001`.
4. **Secret storage:** encrypted in the database ([SYSTEM_DESIGN.md §10](SYSTEM_DESIGN.md)) vs
   systemd `LoadCredential`, never in the environment ([docs/BACKEND_ARCHITECTURE.md § Deployment](docs/BACKEND_ARCHITECTURE.md))
   vs `LoadCredential` or a `0600` env file ([docs/configuration.md](docs/configuration.md));
   PR #8 reads LLM keys from env ([docs/llm-gateway.md](docs/llm-gateway.md)).
5. **Finding categories:** [SYSTEM_DESIGN.md §5.5](SYSTEM_DESIGN.md) vs the frontend set
   used by PR #8 ([docs/llm-gateway.md § Расхождения](docs/llm-gateway.md)).
