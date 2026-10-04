# AGENTS.md

Backend of an AI pull-request reviewer (team 4, DMC-268): a GitHub App that reviews PRs
with an LLM and posts findings back. Python, FastAPI. Rules below are taken from the
repository docs; each cites its source. Do not add rules without a source.

## Layout

Target layout: [docs/BACKEND_ARCHITECTURE.md § Planned code layout](docs/BACKEND_ARCHITECTURE.md).

| Path | Status | Contents |
|---|---|---|
| `docs/`, `SYSTEM_DESIGN.md`, `main.py` | exists | design documents, FastAPI stub |
| `domain/` | lands with PR #8 | models, ports (`domain/ports.py`), errors; pure Python |
| `adapters/` | lands with PR #8 (`adapters/llm/`) | integrations: `db/`, `github/`, `gitlab/`, `stripe/`, `identity/`, `vault/` planned |
| `tests/` | lands with PR #8 | pytest suite |
| `api/` | planned | webhooks, `/v1` routes, auth, app factory |
| `worker/` | planned | claim loop, reaper, periodic tasks |
| `ops/` | planned | `replay.py` |
| `alembic/` | planned | migrations |

## Commands

Tooling lands with PR #8 (`pyproject.toml` on `feat/llm-gateway`):

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
pytest -q
ruff check .
ruff format --check .
mypy domain adapters
```

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
3. **Severity:** not persisted ([docs/configuration.md §3](docs/configuration.md)) vs stored on
   Finding ([SYSTEM_DESIGN.md §8](SYSTEM_DESIGN.md)); the UI shows it on published findings.
4. **Secret storage:** encrypted in the database ([SYSTEM_DESIGN.md §10](SYSTEM_DESIGN.md)) vs
   systemd `LoadCredential`, never in the environment ([docs/BACKEND_ARCHITECTURE.md § Deployment](docs/BACKEND_ARCHITECTURE.md))
   vs `LoadCredential` or a `0600` env file ([docs/configuration.md](docs/configuration.md));
   PR #8 reads LLM keys from env ([docs/llm-gateway.md](docs/llm-gateway.md)).
5. **Finding categories:** [SYSTEM_DESIGN.md §5.5](SYSTEM_DESIGN.md) vs the frontend set
   used by PR #8 ([docs/llm-gateway.md § Расхождения](docs/llm-gateway.md)).
