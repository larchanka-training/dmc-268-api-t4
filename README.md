# DMC-268 API (Team 4)

Backend of an AI pull-request reviewer: a GitHub App that reviews PRs with an LLM and
posts findings back. FastAPI, Python 3.12+.

## Installation

```bash
uv sync --extra dev
```

`uv.lock` pins the whole tree and is committed. Without uv:

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

`requirements.txt` is generated from the lock (runtime only, no dev tools) for a deploy
that has neither — regenerate it rather than editing it by hand:

```bash
uv export --no-dev --no-hashes --no-emit-project -o requirements.txt
```

## Local development

### Context-aware LLM evaluations

`uv run python -m evals run` remains diff-only by default. To provide local head-revision
source trees and/or repository rules for selected cases, pass a JSON manifest:

```json
{
  "sec-sql-sort-injection": {
    "checkout": "./checkouts/sql-case-head",
    "rules": "./rules/review-rules.md"
  },
  "another-case": {
    "checkout": "/path/to/another/head-checkout"
  }
}
```

Paths are resolved relative to the manifest file; checkout paths must be directories and
rules paths must be files. Cases omitted from the manifest receive no extra context. Run
with `uv run python -m evals run --context-manifest eval-context.json`; context mode is
recorded in the summary configuration. This option cannot be combined with `--bundle`.
The evaluator reads only selected, bounded source files, applies the normal AST context
builder/token budget, and never stores or logs source or rules content.

**Warning:** `run` sends case diffs, PR metadata, and any supplied source/rules content to
the configured live LLM provider. Only use trusted data and a provider approved to receive
it. Tests use mocked HTTP transports and never call a live model.

API and PostgreSQL, with throwaway GitHub settings:

```bash
docker compose up --build
curl -fsS http://127.0.0.1:8000/health
```

`GET /health` returns `{"status":"ok"}`. `curl` needs a second terminal while `up` is
in the foreground.

The worker process needs the schema, and migrations never run automatically
on boot (`.agents/rules/backend.md` § Migrations) — apply them once from
inside the image:

```bash
docker compose exec api alembic upgrade head
```

Without it the worker logs `worker.claim.failed` with `error_type=ProgrammingError`
(once per claim slot, until the schema exists) and no job is ever processed. Pulling new migrations? Run the
same command again — `upgrade head` is idempotent.

`docker-compose.yml` is the local stack: it builds from this tree, binds the API and
PostgreSQL to localhost, and fills in throwaway GitHub settings. `deploy/compose.yml`
is the VPS stack: it runs a published image, leaves the API and PostgreSQL on the
compose network, and publishes only Caddy on ports 80 and 443.

On the host, with a real `.env`:

```bash
set -a; . ./.env; set +a          # uvicorn does not read the env file itself
uv run uvicorn main:app --reload --port 8000
uv run pytest -q
```

`--reload` re-imports Python inside the environment uvicorn started with, so a new
variable in `.env` needs a full restart, not a reload.

Hooks match CI (`ruff check`, `ruff format`, `mypy domain adapters`):

```bash
uv run pre-commit install
```
