# Backend rules

Each rule cites its source. `docs/llm-gateway.md`, `docs/specs/llm-gateway.md` and
`tests/conftest.py` arrive with PR #8 (`feat/llm-gateway`).

## Layers and dependency direction

- `adapters/`, `api/`, `worker/` and `ops/` may import `domain/`; `domain/` imports none of
  them, nor any web or database framework
  ([docs/BACKEND_ARCHITECTURE.md § Planned code layout](../../docs/BACKEND_ARCHITECTURE.md)).
- `domain/` has no I/O; its unit tests run without I/O, time goes through an injected clock
  ([docs/BACKEND_ARCHITECTURE.md § Testing strategy](../../docs/BACKEND_ARCHITECTURE.md)).
- Ports are Protocols in `domain/ports.py` (`JobRepository`, `Config`, `GitProvider`,
  `LLMGateway`, `StripeGateway`, `IdentityProvider`, `Clock`); implementations live in
  `adapters/<integration>/`
  ([docs/BACKEND_ARCHITECTURE.md § Planned code layout](../../docs/BACKEND_ARCHITECTURE.md)).
- Vendor differences (message formats, token accounting, API naming) stay inside the
  adapter; the port keeps one shape for every implementation
  ([docs/component-architecture-and-ER-model.md §1](../../docs/component-architecture-and-ER-model.md)).
- The domain never handles a credential: encryption lives in `adapters/vault/`, used by
  adapters, and is not a domain port
  ([docs/BACKEND_ARCHITECTURE.md § Planned code layout](../../docs/BACKEND_ARCHITECTURE.md)).

## Adding an adapter

Follow `adapters/llm/` from PR #8 ([docs/llm-gateway.md](../../docs/llm-gateway.md)):

1. Implement the port Protocol from `domain/ports.py`; the domain does not change
   ([docs/component-architecture-and-ER-model.md §1](../../docs/component-architecture-and-ER-model.md)).
2. Raise only exceptions from `domain/errors.py` to callers, with the provider and the
   reason in the message, never the request body or a key ([docs/llm-gateway.md](../../docs/llm-gateway.md)).
3. Read settings in one `config.py` (`from_env()` into a frozen dataclass); a missing
   required variable is an error naming that variable ([docs/llm-gateway.md](../../docs/llm-gateway.md)).
4. Wire the object graph in `factory.py`; take HTTP transport and clock as injectable
   parameters so tests can replace them ([docs/llm-gateway.md](../../docs/llm-gateway.md)).
5. Log provider, model, duration and counts; never diff text, prompts, model replies or keys
   ([docs/llm-gateway.md § Логирование](../../docs/llm-gateway.md)).
6. Once a port has two adapters, run one conformance suite against both
   ([docs/BACKEND_ARCHITECTURE.md § Testing strategy](../../docs/BACKEND_ARCHITECTURE.md)).

## Configuration and secrets

- Runtime review policy lives in PostgreSQL, per repository
  ([docs/configuration.md](../../docs/configuration.md), intro).
- Never in the config tables: the GitHub App private key, webhook secret, LLM API keys and
  `DATABASE_URL`; `worker_concurrency` and `pool_size` are startup settings, not config rows
  ([docs/configuration.md](../../docs/configuration.md), intro).
- LLM gateway environment (full table in [docs/llm-gateway.md § Переменные окружения](../../docs/llm-gateway.md)):
  - secret: `LLM_PRIMARY_API_KEYS`, from GitHub secret `secrets.AI_DMC268_T4` in CI;
  - GitHub Variable: `LLM_PRIMARY_BASE_URL`, from `vars.AI_DMC268_URL` in CI;
  - plain settings: model ids, fallback switch and URL, timeout, temperature, JSON mode.
- Every environment gets distinct credentials; a dev secret must not authenticate anywhere
  real ([docs/BACKEND_ARCHITECTURE.md § Deployment](../../docs/BACKEND_ARCHITECTURE.md)).
- How secrets reach the process in production (env, `LoadCredential`, encrypted DB) is an
  open question; see [AGENTS.md](../../AGENTS.md#open-questions).

## Tests

- Runner: `pytest`, async tests via `pytest-asyncio`
  ([docs/specs/llm-gateway.md](../../docs/specs/llm-gateway.md)).
- No network. HTTP adapters are tested with `httpx.MockTransport`; forge adapters use
  recorded fixtures ([docs/specs/llm-gateway.md](../../docs/specs/llm-gateway.md);
  [docs/BACKEND_ARCHITECTURE.md § Testing strategy](../../docs/BACKEND_ARCHITECTURE.md)).
- Fake keys look like `sk-test-…` (`tests/conftest.py`, PR #8); a test asserts that no full
  key appears in logs or exception text ([docs/specs/llm-gateway.md](../../docs/specs/llm-gateway.md), test 19).
- No `sleep` in tests: inject a clock
  ([docs/BACKEND_ARCHITECTURE.md § Testing strategy](../../docs/BACKEND_ARCHITECTURE.md)).
- Queue behaviour (`SKIP LOCKED`, leases, reaper) is tested on real PostgreSQL, not a fake;
  fingerprinting has golden tests
  ([docs/BACKEND_ARCHITECTURE.md § Testing strategy](../../docs/BACKEND_ARCHITECTURE.md)).
- The `domain/` import boundary is checked by a test, not by review
  ([docs/BACKEND_ARCHITECTURE.md § Testing strategy](../../docs/BACKEND_ARCHITECTURE.md)).

## Migrations

- Apply with `alembic upgrade head` behind a manual gate; never automatically on boot
  ([docs/BACKEND_ARCHITECTURE.md § Deployment](../../docs/BACKEND_ARCHITECTURE.md)).
- Tenancy, billing and auth tables: `alembic revision --autogenerate`. Review-domain tables
  are hand-written, because autogenerate misses the `updated_at` trigger and `DESC` indexes
  ([docs/db_models_and_migrations.md §4](../../docs/db_models_and_migrations.md)).
- `adapters/db/__init__.py` must import every model module before autogenerate runs, or
  Alembic proposes dropping the missing tables
  ([docs/db_models_and_migrations.md §4](../../docs/db_models_and_migrations.md)).
- `0001` stays editable until the first real deploy, then it is frozen
  ([docs/db_models_and_migrations.md §6](../../docs/db_models_and_migrations.md)).
- CI runs an upgrade/downgrade round trip
  ([docs/BACKEND_ARCHITECTURE.md § Testing strategy](../../docs/BACKEND_ARCHITECTURE.md)).
- Column lists live only in `docs/db_models_and_migrations.md`; do not duplicate them
  elsewhere ([docs/BACKEND_ARCHITECTURE.md](../../docs/BACKEND_ARCHITECTURE.md), intro).
