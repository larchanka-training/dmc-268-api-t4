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

```bash
set -a; . ./.env; set +a          # uvicorn does not read the env file itself
uv run uvicorn main:app --reload --port 8000
uv run pytest -q
```

`--reload` re-imports Python inside the environment uvicorn started with, so a new
variable in `.env` needs a full restart, not a reload.
