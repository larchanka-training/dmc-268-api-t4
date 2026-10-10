# Plan: PostgreSQL queue — migration 0001 + PostgresJobStore (Context Level 1)

Branch: `feat/github-webhook-diff`. Decision record for reviewers: the database
half of the queue (WORKFLOW_DESIGN §1, team decision 2026-10-04); the worker
half lands next.

## Decisions (2026-10-10)

1. **Scope of `0001`:** `pr_review_jobs` + `pr_review_steps` only, no FKs
   beyond steps → jobs — tenancy/billing/auth/config tables do not exist yet
   (docs/db_models_and_migrations.md §1.1 defers them), and `0001` stays
   editable until first deploy (§6).
2. **D4/D5 accepted** (docs/PIPELINE_SPEC.md §9): PR metadata (D5 —
   `pr_title`, `author_login`, `head_ref`, `base_ref`) is in `0001`, all four
   arrive in the webhook payload; severity/category (D4) land with the
   `pr_review_comments` migration, deliberately not `0001`, which creates no
   comments table.
3. **Fairness** keyed on `installation_id` until tenancy lands
   (`MAX_IN_FLIGHT_PER_INSTALLATION`, `domain/jobs.py`), not `account_id`.
4. **Two-PR split:** this PR is migration + `PostgresJobStore` behind the
   unchanged `JobRepository` port (one conformance suite, both adapters —
   .agents/rules/backend.md § Adding an adapter); the worker process lands
   next.

## Enqueue ordering

Dedup runs **before** supersede, not the docs' literal SQL order
(docs/db_models_and_migrations.md §5): in that order a redelivered webhook
would supersede its own PR's active jobs, while dedup-first keeps a duplicate
a no-op (see `adapters/db/repository.py` `enqueue`).

## Deviations from docs/db_models_and_migrations.md

- `installation_id` is `BIGINT` (the forge's numeric id), not a UUID FK into
  `installations` — that table arrives with tenancy.
- `ix_pr_review_jobs_inflight` indexes `installation_id`, not `account_id`;
  no `ix_pr_review_jobs_account_history` either — no `account_id` column yet.
- `stats` is JSONB, an addition to the docs schema: parsed-diff counts written
  by the worker (counts only, never diff content — AGENTS.md rule 3).

## Follow-ups

- Worker: claim loop (`LISTEN`/`NOTIFY`), reaper, periodic tasks.
- Webhook wiring switches from `MemoryJobStore` to `PostgresJobStore`.
- `pr_review_comments` migration with the D4 severity/category columns.
- `account_id` fairness index + backfill when tenancy lands.

## Verification gate (per AGENTS.md rule 7)

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy domain adapters
uv run mypy worker api
```
