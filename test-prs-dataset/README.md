# Gold dataset for the LLM review eval

25 synthetic pull-request diffs with known defects, clean controls and one prompt-injection
case. They feed `python -m evals`; the metric definitions are in
[docs/TEST_PLAN.md § LLM quality method](../docs/TEST_PLAN.md#3-llm-quality-method).

Every diff is hand-written. None comes from a customer or a real repository, and a fixture
key looks like `sk-test-…`.

## Layout

One directory per case, named by its id:

```text
test-prs-dataset/<id>/
  case.yaml     # what the case is and what the review must find
  change.diff   # unified git diff, the only input the model sees besides title/description
```

`sec-sql-sort-injection` points `fixture:` at `tests/fixtures/sql_injection.diff`, which the
prompt unit tests also use.

## `case.yaml`

| Field | Meaning |
|---|---|
| `id` | Equals the directory name. |
| `intent` | One line: what the case checks. |
| `pattern` | The real bug pattern the case models (CWE or a common name). |
| `metrics` | `detection`, `negative control` or `schema-only`. |
| `fixture` | Path to the diff, relative to the case directory. |
| `title`, `description` | Untrusted PR metadata sent to the model. `description` may be empty. |
| `expected` | Defects the review must report: `path`, `line`, `side`, `category`, `severity`. |
| `must_not` | Optional regions that are clean: `path`, with `first_line` and `last_line` for a range. |
| `tags` | Optional. `prompt-injection` marks the injection case. |

A predicted finding matches an expected defect on `path`, `line`, `side` and `category`;
a different `severity` is a severity error, not a miss. Only a `detection` case lists `expected`.

## Cases

| Group | Count | Cases |
|---|---|---|
| security | 6 | `sec-*` |
| correctness | 5 | `cor-*` |
| concurrency | 3 | `con-*` |
| performance (incl. leaks) | 3 | `perf-*` |
| maintainability | 3 | `mnt-*` |
| negative controls | 3 | `neg-*` |
| prompt injection | 1 | `inj-*` (a detection case: the defect must still be found) |
| schema-only | 1 | `fmt-*` (a wide change; scores format validity and dropped findings only) |

## Adding a case

1. Create `<id>/` with `change.diff` and `case.yaml`.
2. Take `line` from the **new** file for `side: new` and from the **old** file for `side: old`.
   It must be an added or removed line, never a context line.
3. Run `uv run python -m evals validate` (also part of `uv run pytest -q`). The loader rejects
   an expected line that is not in the diff, a context line, and an `expected` list on a
   negative control.

## Running

```bash
uv run python -m evals validate                      # no network, no keys
uv run python -m evals run --only sec-sql-sort-injection   # live: needs the LLM_PRIMARY_* variables
```

The full live run is the manual `LLM eval` workflow. It prints schema-valid rate, precision and
recall to the job summary. Output holds counts and finding keys only, never message text.
Version 1 is report-only and runs each case once, so small differences between runs are noise.
