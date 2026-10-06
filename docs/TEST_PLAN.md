---
title: Test Plan
doc_type: test-plan
system: pr-review-bot
status: proposed
version: 1.0
updated: 2026-10-06
owners: [dmc-268-team-4]

purpose: >
  How the review backend is tested: four levels, the LLM quality method,
  the stands, and the scenario templates. Layer choices that this plan
  follows live in the architecture document. The gold corpus and the eval
  harness are produced under issue #27 to the contract in this file.

audience: [backend-engineers, coding-agents, reviewers]
related:
  - path: ./BACKEND_ARCHITECTURE.md
    anchor: testing-strategy
    covers: which layer is unit, contract, or integration, and why
  - path: ../.github/workflows/ci.yml
    covers: default CI, no LLM secret
  - path: ../.github/workflows/llm-smoke.yml
    covers: the live-model workflow
---

# Test Plan

This plan specifies how the review backend is tested. [Backend Architecture § Testing strategy](./BACKEND_ARCHITECTURE.md#testing-strategy) chooses the layer for each concern: domain units, golden fingerprints, recorded-fixture contracts, one conformance suite per port, real PostgreSQL for the queue, webhook goldens, and per-role authorization including a cross-tenant attempt. This file specifies the four [levels](#2-levels), the [LLM scoring method](#3-llm-quality-method), the [stands](#4-stands-and-synthetic-data), and the shape of a [scenario](#5-scenario-templates).

Issue #27 produces the 20–30 case corpus, the `test-prs-dataset/` tree, the eval harness, and the CI script that prints schema-valid rate, precision, and recall. Those artifacts follow the contract in [LLM quality method](#3-llm-quality-method) and [Stands and synthetic data](#4-stands-and-synthetic-data).

## 1. Purpose and boundaries

| Document | Specifies |
| :--- | :--- |
| [Backend Architecture § Testing strategy](./BACKEND_ARCHITECTURE.md#testing-strategy) | Which kind of test each layer gets |
| This file | Levels, LLM metrics, stands, scenario templates |
| Issue #27 | The corpus, the harness, and the eval CI script, built to this contract |

Every test obeys these rules:

- Pytest opens no network. An HTTP adapter receives `httpx.MockTransport` or a recorded fixture ([AGENTS.md](../AGENTS.md), [`.agents/rules/backend.md`](../.agents/rules/backend.md)).
- Model output is parsed into `ReviewOut` and then `ReviewResult`. A reply that does not parse is dropped and is never posted ([`adapters/llm/schema.py`](../adapters/llm/schema.py)).
- Diff text, the PR title, and the PR description are untrusted data. `build_messages` places them between one start marker and one end marker. A marker string inside that data is escaped before it is inserted ([`adapters/llm/prompt.py`](../adapters/llm/prompt.py), [`adapters/llm/prompts/review_v1.md`](../adapters/llm/prompts/review_v1.md)).
- Raw diffs, prompts, and model replies are neither stored nor written to logs. A log line carries provider, model, duration, and counts. A secret appears only as a mask (`sk-…abcd`).

A case that calls a live model is an LLM-evaluation case and runs on the smoke workflow in [LLM stand](#llm-stand). Pytest does not call Eurouter or Ollama.

## 2. Levels

### Unit

A unit test calls a pure function. It opens no socket and no database. It reads the clock only through an injected clock (`FakeClock` in [`tests/conftest.py`](../tests/conftest.py), `FixedClock` for auth). There is no `sleep`.

Implement these cases:

- Domain behaviour for repositories, sign-in, session, and `return_to`, under [`tests/domain/`](../tests/domain/). A `return_to` value is kept only when it is a same-origin path; every other value becomes `None`.
- Schema parsing in [`tests/test_llm_schema.py`](../tests/test_llm_schema.py), against a recorded JSON string, with no provider:
  - A valid review object maps to domain `Finding` values, field for field, and `dropped_findings` is 0.
  - A body wrapped in a ` ```json ` fence parses as the same object.
  - One finding with an unknown severity is dropped; a sibling valid finding is kept.
  - `line` below 1, an empty message, an over-long path, or an over-long suggestion drops that finding.
  - `suggestion: null`, or a missing suggestion, becomes `suggestion is None`.
  - Invalid JSON followed by valid JSON succeeds and performs exactly two provider calls.
  - Invalid JSON twice raises `LLMOutputError`.
  - A reply that is not a review object raises `InvalidReviewJSONError`.
  - Past `FINDINGS_MAX`, the parser keeps the most severe findings.
- Prompt construction in [`tests/test_llm_prompt.py`](../tests/test_llm_prompt.py). `build_messages` returns a system message and a user message. The user message contains the title, the description, and the diff between exactly one `UNTRUSTED_START` and one `UNTRUSTED_END`. A title, description, or diff that itself contains a marker is stored with that marker rewritten to `<<ESCAPED:…>>`, and the real end marker stays at the end of the user message. Ordinary `<<<` text is left unchanged. The system prompt lists each `Severity`, `Category`, and `DiffSide` value.
- Key pool and settings in [`tests/test_llm_keys.py`](../tests/test_llm_keys.py), [`tests/test_llm_config.py`](../tests/test_llm_config.py), and [`tests/test_llm_provider.py`](../tests/test_llm_provider.py). Three keys are issued round-robin. A 429 with `Retry-After` cools that key down until the injected clock passes the deadline, and the same call is retried on the next key. A 401 disables that key for the process lifetime. When every key is unavailable the call raises `ProviderUnavailableError`. A missing `LLM_PRIMARY_API_KEYS` or `LLM_PRIMARY_MODEL` raises an error that names the variable. Fallback enabled without `LLM_FALLBACK_MODEL` is an error. A keyless provider sends no `Authorization` header; a keyed provider sends `Bearer`.
- The import boundary in [`tests/test_domain_purity.py`](../tests/test_domain_purity.py). Walk every `domain/*.py` with `ast`. The top-level import root must not be `adapters`, `api`, `worker`, `ops`, `fastapi`, `starlette`, `pydantic`, `httpx`, `sqlalchemy`, or `alembic`.
- Secrets in [`tests/test_llm_secrets.py`](../tests/test_llm_secrets.py). With `caplog`, no log record and no exception text contains a full key. `LLMSettings` repr hides keys. Fixture keys look like `sk-test-…`.

### Integration

An integration test drives one boundary. The far side is a fake, except for the queue, where the far side is PostgreSQL.

Implement HTTP and API cases with `httpx.MockTransport` and the JSON files in [`tests/fixtures/github/`](../tests/fixtures/github/). FastAPI `TestClient` drives [`tests/api/`](../tests/api/). In-memory fakes under [`tests/ports/`](../tests/ports/) implement the port operations (session store, sign-in attempt store, repositories cache) so a later PostgreSQL adapter runs the same suite.

Auth cases refuse a missing, stale, or replayed `state`, drop an unsafe `return_to`, set the session cookie attributes, and reject logout without the CSRF header or from a foreign origin. No log record from the callback contains the authorization code, the state, or a token. Repository routes require a session, page by the cursor, and return an empty page when the installation is gone, without calling GitHub for a user who has no organisation.

The GitHub App adapter mints an RS256 JWT from the fixture PEM, backdates `iat`, sets `exp` nine minutes out, and moves both claims with the injected clock. An installation token is cached until the renew margin, reminted inside that margin, and dropped by `forget`. An uninstalled or suspended installation raises the gone error; a timeout or a body without a token raises forge-unavailable. Neither the private key nor a token appears in a repr or a log record.

Queue cases run against the PostgreSQL service in [`docker-compose.yml`](../docker-compose.yml). Claim with `FOR UPDATE SKIP LOCKED`, lease expiry, and the reaper are exercised on that engine. A fake database is not a substitute. CI runs `alembic upgrade head` and the matching downgrade as a round trip. Once a port has two adapters, one conformance suite runs against each of them.

Webhook cases replay a golden payload per provider, including a payload whose signature does not verify. API authorization cases include one deliberate cross-tenant read, which must stay refused.

### End to end

An end-to-end case starts at a process boundary and checks a visible outcome.

The image job in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) builds `dmc268-api:ci` and starts it with a throwaway RSA key mounted at `/run/app.pem` (mode `644`, so uid 10001 inside the image can read it). The environment is `APP_ORIGIN=http://localhost:8000`, `GITHUB_CLIENT_ID=ci`, `GITHUB_CLIENT_SECRET=ci`, `GITHUB_CALLBACK_URL=http://localhost:8000/auth/github/callback`, `GITHUB_APP_ID=1`, `GITHUB_APP_PRIVATE_KEY_PATH=/run/app.pem`, `GITHUB_APP_SLUG=ci`. The job polls `GET /health` up to ten times, two seconds apart, and passes only when the body is `{"status":"ok"}`. The container process uid is not 0.

A development-phase review case is a synthetic merge request: a diff file, a title, and a description checked into the repo. It is a fixture, not a pull request on a customer repository. Driving a signed webhook through to a posted comment uses a scratch repository on the manual stand in [LLM stand](#llm-stand), and that path stays off the default CI job.

### LLM evaluation

Keep two tracks.

**Offline.** Schema adherence and prompt delimiters, implemented as the unit cases in [Unit](#unit). They construct `ReviewRequest` from fixture text and parse a recorded reply. They perform no HTTP call to a model.

**Online.** A live model runs only in [`.github/workflows/llm-smoke.yml`](../.github/workflows/llm-smoke.yml), on `workflow_dispatch`, with `concurrency.group` `llm-smoke` and `cancel-in-progress: false`. The job installs the package, then runs:

```text
python -m adapters.llm.smoke tests/fixtures/sql_injection.diff \
  --title "Add a sortable users list"
```

with `LLM_PRIMARY_API_KEYS` from secret `AI_DMC268_T4`, `LLM_PRIMARY_BASE_URL` from `vars.AI_DMC268_URL`, `LLM_PRIMARY_MODEL` from `vars.LLM_PRIMARY_MODEL`, and `LLM_TIMEOUT_SECONDS=300`. Standard output, which holds the findings text, goes to a runner temp file. Standard error, which holds provider, model, duration, counts, or the error line, is printed. The job summary records provider, model, finding count, and counts by severity and category. The finding message stays out of the summary. Exit is failure when the process fails or when `findings` is empty.

The corpus and harness from issue #27 run on this same stand and score each case with [LLM quality method](#3-llm-quality-method). One case remains the SQL-injection fixture above; the harness adds the rest of the corpus around it.

## 3. LLM quality method

Score `ReviewResult.findings` from [`domain/models.py`](../domain/models.py). Leave the summary string and the log line out of the score. Precision and recall are computed from the review result. A log line contains the provider, the model, the duration, and the finding count.

A finding has a position (`path`, `line` ≥ 1, `side` of `old` or `new`), a `severity` (`critical`, `high`, `medium`, `low`), a `category` (`security`, `correctness`, `concurrency`, `performance`, `maintainability`), a `message` (1–2000 characters), and an optional `suggestion`.

### Match

A predicted finding matches an expected defect when `path`, `line`, `side`, and `category` are equal. Leave `message` and `suggestion` out of the key: wording moves between runs.

Each expected defect matches at most one predicted finding, and each predicted finding matches at most one expected defect. Pair them greedily by that key.

A severity that differs on an otherwise matched pair is a **severity error**. Count the pair as a true positive, and report severity errors beside precision and recall.

### Counts

| Term | Meaning |
| :--- | :--- |
| True positive (TP) | A predicted finding that matches one expected defect |
| False positive (FP) | A predicted finding with no match. On a clean fixture every predicted finding is a false positive |
| False negative (FN) | An expected defect with no match |
| Hallucination | A false positive whose `path` is absent from the diff, or whose `line` lies outside every changed hunk on that `side` |

Hallucinations are a subset of false positives. Include them in precision and report their count on its own.

Changed hunks are the lines the diff marks added or removed. A finding on a context line (a line prefixed with a space in the unified diff) is outside the hunk and is a hallucination.

### Rates

- **Precision** = TP / (TP + FP). When TP + FP = 0, precision is 1.
- **Recall** = TP / (TP + FN).
- A case with an empty expected set is a **negative control**. Report precision and the hallucination count. Recall on that case is 1 when FP = 0. When FP > 0, omit recall for that case and leave the case out of corpus recall.
- **Schema-valid rate** = (runs whose reply parses to `ReviewOut` after the gateway’s one corrective retry) / (runs). `parse_review` accepts a fenced ` ```json ` body. A reply that is not a review object is `InvalidReviewJSONError`. The gateway retries that error once, then moves on.
- **Dropped-finding count** = findings the parser discarded (unknown severity or category, `line` < 1, empty message, path or suggestion over the schema limit, or the overflow past `FINDINGS_MAX`). Record a dropped finding in this count. A dropped finding is a parser event, separate from “the model found no bug.”

Corpus precision sums TP and FP over every detection case and every negative control. Corpus recall sums TP and FN over detection cases only.

### Runs

Set `LLM_TEMPERATURE` to `0.0`. An identical prompt can still yield different findings ([Backend Architecture § Observability](./BACKEND_ARCHITECTURE.md#observability)). Version 1 records one run per case and prints that limit next to the numbers. Repeated runs and confidence intervals belong to a later revision of the harness.

## 4. Stands and synthetic data

### CI stand

The `checks` job in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs ruff, `mypy domain adapters`, and `pytest`. The `image` job is the end-to-end health check in [End to end](#end-to-end). Neither job receives `AI_DMC268_T4` or any other LLM key.

### Local stand

[`docker-compose.yml`](../docker-compose.yml) runs the API on `127.0.0.1:8000` and PostgreSQL 17 on `127.0.0.1:5432` (user `dmc`, password `dmc`, database `dmc268`). The API environment is `APP_ORIGIN=http://localhost:8000`, `GITHUB_CLIENT_ID=local`, `GITHUB_CLIENT_SECRET=local`, `GITHUB_CALLBACK_URL=http://localhost:8000/auth/github/callback`, `GITHUB_APP_ID=1`, `GITHUB_APP_SLUG=local`. The entrypoint writes a throwaway PKCS8 PEM to `/tmp/dev-app.pem` and then execs uvicorn. These values authenticate nowhere real. Queue integration tests use this Postgres. Staging and production use different credentials.

### LLM stand

Live review runs on `workflow_dispatch` of [`.github/workflows/llm-smoke.yml`](../.github/workflows/llm-smoke.yml), with the secret and variables in [LLM evaluation](#llm-evaluation). Prompt tuning against a scratch repository uses the replay entry point with `--repo you/test-repo --pr 42`, as in the architecture document. The same workflow definition is the live stand; there is no second environment.

### Synthetic merge-request contract

Each case is a set of files in the repository. It is not a GitHub pull request. The files carry the fields of the LLM-eval template in [Scenario templates](#5-scenario-templates).

The corpus contains:

- known defects in `security`, `correctness`, and at least one other `Category`
- one clean diff (empty `expected`, a negative control)
- one diff that places instruction-like text in the untrusted title, description, or diff; the review treats that text as data

The set is large enough to compute the rates in [Rates](#rates) and small enough that a reviewer can check every expected defect by hand. Issue #27 targets 20–30 cases.

A case contains no customer diff, no real API key, and no production prompt or response. A fixture key, when a case needs one, looks like `sk-test-…`.

## 5. Scenario templates

Write each scenario with the fields below. `level` is `unit`, `integration`, `e2e`, or `llm-eval`.

### Unit or integration

```text
### <id>
- level: unit | integration
- intent: <one sentence>
- preconditions: <clock, fixtures, fakes>
- input: <function call, HTTP request, or fixture path>
- steps: <what the test does>
- expected: <assertion>
- network: forbidden
```

#### `schema-sql-injection-finding`

- **level:** unit
- **intent:** A recorded model reply that names the SQL injection becomes one domain `Finding`.
- **preconditions:** The finding dict in [`tests/conftest.py`](../tests/conftest.py) (`path` `app/users.py`, `line` 16, `side` `new`, `severity` `critical`, `category` `security`, message about interpolating `sort` into SQL, plus a before/after suggestion). No provider and no key.
- **input:** `parse_review` on `json.dumps` of `{"summary": "Adds a users endpoint.", "findings": [<that dict>]}`.
- **steps:** Parse the JSON. Map `ReviewOut` through `to_domain()`.
- **expected:** `dropped_findings == 0`. The only finding equals that dict as a `Finding`: position `app/users.py` line 16 side `new`, severity `critical`, category `security`, the same message, and the suggestion tuples.
- **network:** forbidden. Implement the call as a direct function call. The online track uses [`tests/fixtures/sql_injection.diff`](../tests/fixtures/sql_injection.diff) with `adapters.llm.smoke`. An offline HTTP test, when one is needed, mounts `httpx.MockTransport` (the `RecordingTransport` helper in `tests/conftest.py`).

### LLM evaluation

```text
### <id>
- level: llm-eval
- intent: <detection | negative control | schema-only>
- fixture: <path to the diff>
- title: <untrusted title>
- description: <untrusted description, or empty>
- expected:
    - path: <repo-relative path>
      line: <int >= 1>
      side: old | new
      category: security | correctness | concurrency | performance | maintainability
      severity: critical | high | medium | low
- must_not:
    - <path, or path plus a line range, that is clean>
- metrics: <detection | negative control | schema-only>
- stand: workflow_dispatch llm-smoke; one run; finding text stays out of the log
```

`metrics: detection` contributes to precision and recall. `metrics: negative control` contributes to precision and the hallucination count, with `expected` empty. `metrics: schema-only` contributes to schema-valid rate and the dropped-finding count, and adds nothing to TP, FP, or FN.

#### `eval-sql-sort-injection`

- **level:** llm-eval
- **intent:** detection — the review reports the string-built `ORDER BY`.
- **fixture:** [`tests/fixtures/sql_injection.diff`](../tests/fixtures/sql_injection.diff)
- **title:** `Add a sortable users list`
- **description:** empty
- **expected:**
  - path: `app/users.py`
  - line: 16
  - side: `new`
  - category: `security`
  - severity: `critical`

  Line 16 is the added `conn.execute(f"SELECT id, name, email FROM users ORDER BY {sort}")`. The hunk header `@@ -1,13 +1,20 @@` places that statement on line 16 of the new file.
- **must_not:**
  - any path other than `app/users.py` (the diff touches only that file)
  - `app/users.py` lines 1–10 and 18–20: `get_connection`, the blank context under the new function, and `@app.get("/health")`
- **metrics:** detection. A match on the expected row is a TP. A finding on another path, or on a context line, is a hallucination and an FP. An extra finding inside the added hunk (lines 11–17) that misses the expected key is an FP and is not a hallucination.
- **stand:** `workflow_dispatch` on `llm-smoke`, one run. The job summary shows provider, model, and counts by severity and category. The finding message stays in the runner temp file.

## 6. Suite

Build the suite as the cases in [Levels](#2-levels), scored and hosted as in [LLM quality method](#3-llm-quality-method) and [Stands and synthetic data](#4-stands-and-synthetic-data). The modules below are the layout. Each bullet is the behaviour the module implements.

**Unit**

- [`tests/domain/`](../tests/domain/) — pure domain rules, injected clock.
- [`tests/test_llm_schema.py`](../tests/test_llm_schema.py) — `parse_review` and the one corrective retry inside `review_with_provider`.
- [`tests/test_llm_prompt.py`](../tests/test_llm_prompt.py) — markers and escaping, using `tests/fixtures/sql_injection.diff` as the diff body.
- [`tests/test_llm_keys.py`](../tests/test_llm_keys.py), [`tests/test_llm_config.py`](../tests/test_llm_config.py), [`tests/test_llm_provider.py`](../tests/test_llm_provider.py) — pool, settings, and HTTP status handling behind `MockTransport`.
- [`tests/test_llm_secrets.py`](../tests/test_llm_secrets.py) — full keys absent from logs, errors, and repr.
- [`tests/test_domain_purity.py`](../tests/test_domain_purity.py) — the `ast` import walk.

**Integration**

- [`tests/adapters/`](../tests/adapters/) — GitHub identity and App authentication against `tests/fixtures/github/`.
- [`tests/api/`](../tests/api/) — `TestClient` auth, setup callback, and repository routes.
- [`tests/ports/`](../tests/ports/) — the in-memory conformance cases for session, sign-in attempt, and repositories cache. The PostgreSQL adapters of those ports run this same suite.
- Queue module on the compose database: claim, lease expiry, reaper.
- CI migration job: upgrade to head, downgrade, upgrade to head.
- Webhook golden payloads, including a bad signature.
- One cross-tenant authorization case that stays refused.
- A second conformance module per port when that port gains a second adapter.

**End to end**

- [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) image job: `/health` returns `{"status":"ok"}`, process uid is not 0.
- Manual scratch-repository run: signed webhook to a posted comment, off the default CI job.

**LLM evaluation**

- Offline cases are the schema and prompt modules above.
- Online case `eval-sql-sort-injection` on `llm-smoke`, as in [`eval-sql-sort-injection`](#eval-sql-sort-injection).
- Issue #27 adds `test-prs-dataset/` and a script that prints schema-valid rate, precision, recall, hallucination count, and dropped-finding count for one run of each case, using the match rule in [Match](#match).
