# Plan: GitHub Webhook + VCS client + diff parser (Context Level 1)

Branch: `feat/github-webhook-diff` (already exists, identical to `main`).

## Accepted decisions (user, 2026-10-07)

1. **Queue:** in-memory `JobRepository` port implementation now
   (`adapters/jobs/memory.py`); PostgreSQL adapter (`adapters/db/jobs.py`,
   DDL from `docs/db_models_and_migrations.md`) is a follow-up PR.
2. **Processing model:** webhook validates → enqueues → responds `202`
   immediately (WORKFLOW_DESIGN §2 Step 3, no network I/O in the request
   path); an `asyncio.create_task` background job (future claim-loop worker
   Step 5) fetches diff + metadata, parses and filters. Only metadata/stats
   are recorded on the job — diff content is never stored or logged
   (AGENTS.md hard rule 3).
3. **Providers:** GitHub only; ports (`GitProvider`) stay provider-neutral.
   GitLab needs a credential store first (WORKFLOW_DESIGN §2 Step 5).

## Architecture

```
POST /webhooks/github            api/webhooks/github.py
  ├─ raw body ≤5MB (413)         adapters/github/signature.py  HMAC-SHA256 (401)
  ├─ X-GitHub-Event routing      adapters/github/payload.py    action filter (204)
  ├─ enqueue                     adapters/jobs/memory.py       dedup + supersede
  ├─ asyncio.create_task ──►     worker/process.py
  └─ 202                            ├─ adapters/github/client.py   .diff + metadata + commits
                                    ├─ domain/diff.py              parse + chunk
                                    ├─ domain/diff_filter.py       skip binary/lock/minified
                                    └─ job COMPLETED(stats) / FAILED(error_kind)
```

## Components

### domain/ (pure, no I/O)

- `domain/diff.py` — `DiffLine`, `DiffHunk`, `FileDiff`, `parse_diff(text)`,
  chunking (`merge_gap=3` context lines, `max_chunk_lines=400`), line-number
  invariants (added→new, deleted→old, context→both). Malformed input →
  `DiffFormatError` (new, in `domain/errors.py`).
- `domain/diff_filter.py` — `SkipReason` (binary/lockfile/minified/vendored),
  `filter_files(files) -> (kept, skipped)`. Lockfiles by exact name
  (package-lock.json, yarn.lock, pnpm-lock.yaml, poetry.lock, uv.lock,
  Pipfile.lock, Cargo.lock, go.sum, composer.lock, Gemfile.lock, flake.lock,
  npm-shrinkwrap.json, bun.lockb), minified (`*.min.js`, `*.min.css`,
  `*.min.mjs`, `*.map`), vendored (any path segment in {vendor,
  node_modules, third_party, third-party}). Empty kept → caller records `skip_no_reviewable_files`.
- `domain/ports.py` — new DTOs and ports:
  - `GitProvider` protocol: `fetch_pull_request(installation_id,
    repo_full_name, number) -> PullRequestContext` (title, description,
    head/base sha+ref, author, commits with messages, `diff_text`).
  - `JobRepository` protocol: `enqueue(job) -> job_id | None` (dedup on
    `(provider, delivery_id)`, supersede active jobs of same PR), and
    `mark_processing` / `mark_completed(stats)` / `mark_failed(error_kind)`.
  - Status machine QUEUED→PROCESSING→COMPLETED/FAILED/SUPERSEDED.
- No new dependencies (hmac is stdlib).

### adapters/github/ (mirror adapters/llm/ conventions)

- `signature.py` — HMAC-SHA256 over raw bytes, `hmac.compare_digest`,
  `sha256=<hex>` header format.
- `payload.py` — `pull_request` payload → normalized event; reviewable
  actions: `opened`, `synchronize`, `reopened`, `ready_for_review`
  (WORKFLOW_DESIGN §2 Step 1 table).
- `client.py` — `GitHubVCSClient(GitProvider)`: reuses `app_auth.py` /
  `installation.py` for installation tokens; `GET /repos/{o}/{r}/pulls/{n}`
  (metadata), `GET …/pulls/{n}/commits` (messages, paginated, capped),
  `GET …/pulls/{n}.diff` (Accept `application/vnd.github.diff`). Errors →
  `ForgeError` family with provider+reason, never body/token.
- `config.py` — add `GITHUB_WEBHOOK_SECRET` (required for webhook wiring,
  `repr=False`).
- `factory.py` — `build_vcs_client(settings, *, transport=None, clock=None)`.
- Logging: provider, installation, repo, pr, sha, counts — never diff text.

### adapters/jobs/

- `memory.py` — `MemoryJobStore(JobRepository)`: dict + asyncio.Lock, dedup,
  supersession, status transition validation. Replaced later by
  `adapters/db/jobs.py` behind the same port.

### worker/ (new package)

- `process.py` — `async process_review_job(job_id, vcs, jobs, *, clock)`:
  PROCESSING → fetch → parse → filter → stats (files total/kept/skipped by
  reason, hunks, chunks) → COMPLETED; on error FAILED with `error_kind`.

### api/

- `webhooks/github.py` — `POST /webhooks/github`: 5MB cap (413), HMAC (401),
  event routing (`pull_request` else 204), action filter (204), enqueue
  (duplicate delivery → still 202), background task, 202.
- CSRF exemption for `/webhooks/*` in `api/deps.py` (no cookies involved).
- `api/main.py` — wire router + memory job store + vcs client; webhook secret
  config; `docker-compose.yml` gains `GITHUB_WEBHOOK_SECRET`.

### Tests (no network; MockTransport/RecordingTransport + fixtures)

- `tests/domain/test_diff_parser.py`, `test_diff_filter.py` (+ chunking).
- `tests/adapters/test_github_signature.py`, `test_github_payload.py`,
  `test_github_vcs_client.py`.
- `tests/adapters/test_memory_jobs.py` — dedup, supersession, transitions.
- `tests/api/test_webhook_github.py` — 401/413/204/202, duplicate delivery,
  secret absent from caplog.
- `tests/test_e2e_context.py` — webhook → 202 → background task → job
  COMPLETED with stats; diff text never in logs.
- Existing `tests/test_domain_purity.py` covers new domain modules
  automatically.

## Task order

Domain parser/filter/chunking → ports → github adapters (signature, payload,
client) → memory jobs → worker → api webhook → e2e → docs (AGENTS.md layout
table) → all five commands green.

## Verification gate (per AGENTS.md rule 7)

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy domain adapters
```

---

# Correctness plan: Python symbol collection Point lifetime

## Overview

Fix the Python tree-sitter collector's use of temporary `Point` wrappers. In fresh subprocesses, reading a chained `node.end_point.row` can produce row `0` instead of `300`; using a retained `Point` local produces the correct row. On a valid function with roughly 300 assignments, Point access during traversal can also terminate the child process with `SIGSEGV`. Preserve the existing Python symbol results and traversal order without shared mutable state or changes to parser ownership.

## Relevant context and constraints

- `TreeSitterSymbolIndex` documents that it is safe to share across jobs (`adapters/context/tree_sitter_index.py`). Keep the fix local to the current call and do not introduce a module-level or instance-level traversal stack/cache.
- Python symbol collection is depth-first and source ordered: append a definition before visiting its named children; a decorated definition visits its final named child with the decorator row as the span start; function descendants retain the current `in_class`; class descendants use `in_class=True`.
- Existing behavioral coverage is in `tests/adapters/test_context_tree_sitter.py` (decorators, class/method kind, nested spans, docstrings, and parse-error behavior). Keep TypeScript/JavaScript and Go collectors out of scope.
- Because the failure may terminate the interpreter rather than raise a Python exception, the large-input regression must run parsing in a child process. The pytest runner itself must survive a reproduced crash.

## Architecture decision

Keep the existing recursive depth-first traversal. Retain each relevant tree-sitter `Point` in a local before reading `.row`, and keep the definition/decorator points alive while traversing that node's children. Avoid point reads on generic syntax nodes. Apply the same retained-local pattern in `_header_end`, `_python_docstring_end`, and parse-error/call-line checks where point rows are read before further node traversal. This preserves source order and avoids adding traversal state.

## Test-first implementation order

1. Add a subprocess-isolated regression for a valid function containing approximately 300 assignment statements. It should invoke the public `TreeSitterSymbolIndex` API, assert a successful child exit and expected end row, and use a finite timeout. Confirm the test fails on the current implementation without allowing a child crash to kill pytest.
2. Retain Point wrappers in the Python collector/helpers and avoid unnecessary Point reads on generic nodes. Re-run the subprocess regression and existing Python context tests; confirm symbol results remain unchanged.
3. Run the parser adapter tests and requested static checks; inspect the final diff for narrow scope and worker-safety regressions.

## Acceptance criteria

- The subprocess regression passes reliably for a valid ~300-assignment function; a child crash or nonzero exit is reported as a normal pytest failure.
- Symbol names, kinds, spans, decorator handling, docstring signature ends, and ordering remain unchanged for existing Python fixtures and representative nested definitions.
- Python traversal remains recursive with Point wrappers held through the relevant child traversal; unrelated language collectors and worker/parser lifecycle do not change.
- The subprocess regression, parser adapter tests, `uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy domain adapters` pass. Run the full pytest suite if requested/needed for the repository gate.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Point wrappers are released before child traversal | Incorrect rows or process-level crash | Keep relevant wrappers in locals for the duration of recursive descent and cover with the subprocess regression |
| Broader behavior changes while fixing Python | Other language collectors or index sharing can regress | Keep the patch in the Python Point-read paths and run adapter tests |
| Regression still crashes pytest | Test process is lost and failure diagnosis is poor | Run the reproducer through `subprocess.run` with captured output and timeout |
| Fix introduces shared state or synchronization changes | Concurrent worker calls could interfere or serialize | Keep the fix local; do not change parser ownership/lifecycle |

## Open questions

- None for the narrow fix; use the existing public index behavior as the compatibility contract.
