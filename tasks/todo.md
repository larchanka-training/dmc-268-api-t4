# Todo — GitHub Webhook + VCS client + diff parser (Context Level 1)

Plan: [tasks/plan.md](plan.md)

- [x] 1. Domain: diff parser + chunking — `domain/diff.py`, `DiffFormatError`
      in `domain/errors.py`, tests `tests/domain/test_diff_parser.py`
- [x] 2. Domain: diff filter — `domain/diff_filter.py`, tests
      `tests/domain/test_diff_filter.py`
- [x] 3. Domain: ports & DTOs — `GitProvider`, `PullRequestContext`,
      `CommitInfo`, `JobRepository`, `NewReviewJob`, `ReviewJob`,
      `ReviewJobStatus`, `JobStats` in `domain/ports.py`
- [x] 4. GitHub signature — `adapters/github/signature.py`, tests
- [x] 5. GitHub payload parsing — `adapters/github/payload.py`, tests
- [x] 6. GitHub VCS client + config + factory — `adapters/github/client.py`,
      `config.py` (+`GITHUB_WEBHOOK_SECRET`), `factory.py`, fixtures, tests
- [x] 7. Memory job store — `adapters/jobs/memory.py`, tests
      (`tests/adapters/test_memory_jobs.py`)
- [x] 8. Worker process function — `worker/process.py`, tests
- [x] 9. API webhook endpoint — `api/webhooks/github.py`, CSRF exemption in
      `api/deps.py`, wiring in `api/main.py`, `GITHUB_WEBHOOK_SECRET` in
      `docker-compose.yml`, tests `tests/api/test_webhook_github.py`
- [x] 10. E2E context test — `tests/test_e2e_context.py`; update AGENTS.md
        layout table (adapters/jobs/, worker/)
- [x] 11. Final verification: pytest (584 passed) / ruff check / ruff format /
       mypy (domain adapters, api worker) all green; review pass with 0
       findings; publish PR

---

# Todo — Python symbol collection Point lifetime

Plan: [Correctness plan in plan.md](plan.md#correctness-plan-python-symbol-collection-point-lifetime)

- [x] 1. **Test first:** Add `test_python_large_function_collection_preserves_end_point_row` in `tests/adapters/test_context_tree_sitter.py` for a valid function with ~300 assignments, captured output, and finite timeout. Verified red before the fix: child exits `-11`; pytest survives and reports failure.
- [x] 2. Fix the Point lifetime root cause: retain Point wrappers in locals before `.row`, keep relevant definition/decorator points alive through child traversal, and avoid Point reads on generic syntax nodes. Keep recursive traversal; iterative traversal remained red and is not needed for the fix.
- [x] 3. Verify Python results and other parser adapters with `uv run pytest -q tests/adapters` (275 passed), including decorated starts, class/method kinds, docstring signature rows, and TypeScript/JavaScript/Go coverage. No shared traversal state or parser lifecycle changes were introduced.
- [x] 4. Verify full repository `uv run pytest -q` (1268 passed, 59 skipped; one Starlette deprecation warning), `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy domain adapters`, and `git diff --check`; all passed.
