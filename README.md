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

API and PostgreSQL, with throwaway GitHub settings:

```bash
docker compose up --build
curl -fsS http://127.0.0.1:8000/health
```

`GET /health` returns `{"status":"ok"}`. `curl` needs a second terminal while `up` is
in the foreground.

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
