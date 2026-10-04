---
name: code-review
description: Use to review a pull request or a local diff in this repository against its architecture, security and testing rules.
---

# Code review

Rules come from [AGENTS.md](../../../AGENTS.md) and
[.agents/rules/backend.md](../../rules/backend.md); each checklist item cites its source.

## Steps

1. Get the diff:
   - a pull request: `gh pr diff <number>`;
   - the local branch: `git fetch origin && git diff origin/main...HEAD`.
2. Read the changed files around each hunk, not only the hunk.
3. Walk the checklist below for every changed file.
4. Report findings in the format below, most severe first. No findings: say so.

## Checklist

- **Layer boundaries.** `domain/` imports nothing from `adapters/`, `api/`, `worker/`,
  `ops/`, or web and database frameworks, and does no I/O; new integrations sit behind a
  port in `domain/ports.py`
  ([docs/BACKEND_ARCHITECTURE.md § Planned code layout](../../../docs/BACKEND_ARCHITECTURE.md)).
- **Secrets.** No key, token or other credential in code, fixtures, docs or config rows. No secret
  or unmasked key in log calls, exception messages, API responses or the prompt
  ([docs/BACKEND_ARCHITECTURE.md § Security model](../../../docs/BACKEND_ARCHITECTURE.md);
  [docs/configuration.md](../../../docs/configuration.md); [docs/llm-gateway.md](../../../docs/llm-gateway.md), PR #8).
- **Retention.** Raw diffs, prompts and model responses are not stored or logged
  ([docs/BACKEND_ARCHITECTURE.md § Secret redaction](../../../docs/BACKEND_ARCHITECTURE.md)).
- **Tests.** New behaviour has tests; no test reaches the network (`httpx.MockTransport`,
  recorded fixtures); no `sleep`, an injected clock instead
  ([docs/BACKEND_ARCHITECTURE.md § Testing strategy](../../../docs/BACKEND_ARCHITECTURE.md);
  [docs/specs/llm-gateway.md](../../../docs/specs/llm-gateway.md), PR #8).
- **LLM output validation.** Model output is parsed into a schema before use; unparseable
  items are dropped, never posted; unknown enum values are not coerced
  ([docs/BACKEND_ARCHITECTURE.md § Prompt injection](../../../docs/BACKEND_ARCHITECTURE.md);
  [SYSTEM_DESIGN.md §5.6](../../../SYSTEM_DESIGN.md); [docs/llm-gateway.md](../../../docs/llm-gateway.md), PR #8).
- **Prompt injection.** Diff, PR title and description stay inside the untrusted-data
  markers and never reach the instruction part; marker look-alikes in the data are escaped;
  model output never triggers an action beyond comment text
  ([docs/BACKEND_ARCHITECTURE.md § Prompt injection](../../../docs/BACKEND_ARCHITECTURE.md)).
- **Migrations.** No automatic `alembic upgrade` on boot; every model module imported in
  `adapters/db/__init__.py`; `0001` edited in place only before the first real deploy
  ([docs/BACKEND_ARCHITECTURE.md § Deployment](../../../docs/BACKEND_ARCHITECTURE.md);
  [docs/db_models_and_migrations.md §4, §6](../../../docs/db_models_and_migrations.md)).
- **Docs consistency.** A change to a contract (port, Finding shape, env variable, schema,
  config key) without the matching update in `docs/` is a finding
  ([docs/BACKEND_ARCHITECTURE.md](../../../docs/BACKEND_ARCHITECTURE.md), intro: one source per fact).
- **Open questions.** Code that silently picks a side on an item in
  [AGENTS.md § Open questions](../../../AGENTS.md#open-questions) is a finding.

## Output format

Severity and category values match `Severity` and `Category` in `domain/models.py`
(PR #8), so agent reviews and the product use one vocabulary.

- severity: `critical` | `high` | `medium` | `low`
- category: `security` | `correctness` | `concurrency` | `performance` | `maintainability`

One block per finding:

```
path/to/file.py:42 · high · security
message: what is wrong, why it matters, how to fix it.
suggestion:
  before: <current lines, verbatim>
  after:  <replacement lines>
```

Leave `suggestion` out when there is no concrete replacement. End with one summary line:
the counts per severity.
