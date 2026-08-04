---
name: verification-gate
description: 'Run the full completion checklist (ruff, mypy, pytest, import-sanity) before marking any task complete or committing. Use before every commit, whether working interactively or as part of the swarmd.py autonomous loop.'
---

# Verification Gate

This is the human/agent-facing checklist version of the gate `swarmd.py`
automates mechanically after every autonomous task (see `AGENTS.md` §4 and
§9, `SWARM.md`). Run it yourself before considering any interactive change
complete too — don't rely on the swarm catching what you skipped.

## Steps

Run from the repo root, with the venv active (`source .venv/bin/activate`):

```bash
python3 -m compileall -q .                    # syntax sanity, fast fail
ruff check .                                  # lint — must be zero warnings
                                               # on files you touched
mypy --ignore-missing-imports .               # advisory (see below)
python3 -m pytest -q                          # full test suite
python3 -c "import webapp.app"                # webapp import-sanity
python3 -c "import main"                      # agent import-sanity
```

If `requirements-web.txt` extras aren't installed in the active venv, the
`webapp.app` import check will fail with a `ModuleNotFoundError` unrelated to
your change — install them (`pip install -r requirements-web.txt`) rather
than skipping the check.

## Pass Criteria

- `compileall`, `ruff check .`, `pytest -q`, and both import-sanity checks
  must all exit zero. This is what `swarmd.py`'s gate treats as blocking.
- `mypy` is **advisory, not blocking** — the codebase doesn't have full type
  coverage yet (`AGENTS.md` §5). Don't introduce *new* mypy errors in files
  you touch, but pre-existing errors elsewhere aren't your problem to fix as
  part of an unrelated task.

## What This Does NOT Catch

- **Multi-tenant leaks** — `ruff`/`mypy`/`pytest` won't tell you that a new
  function forgot to filter by `user_id`. That's a manual review point (see
  `multi-tenant-migration` skill) — a query missing `WHERE user_id = ?` is
  syntactically valid Python and will pass every automated check while
  leaking one user's data to another.
- **Wired-vs-orphaned modules** — tests passing doesn't mean a new module is
  actually called from `main.py`/`webapp/app.py`. Grep for the import
  yourself (`AGENTS.md` §4's "never leave dead orphaned modules" rule).
- **Secret leakage** — no automated check here greps for accidentally-logged
  API keys; do that manually per the `secrets-and-encryption` skill.
- **Real external API behaviour** — the test suite mocks all network calls
  (Hevy, Google Health, Telegram, AI providers) by design (no network access
  in tests). A connector change that passes tests can still be wrong against
  the real API's actual response shape; sanity-check against real API docs
  or a manual `--preview` run when touching connector parsing.
- **Visual/UX correctness** of new Jinja2 templates — a route returning 200
  with the right JSON in a test can still render broken HTML. Load it in a
  browser (`uvicorn webapp.app:app --reload`) for anything template-facing.

## For the Autonomous Swarm Specifically

`swarmd.py`'s `run_tests()` runs exactly the blocking subset of the steps
above (`compileall`, `ruff`, `pytest`, both import checks) after every task.
On failure it runs up to `SWARM_FIX_MAX_ROUNDS` automated fix passes scoped
to the failing files; if still failing, it discards the working tree changes
entirely and moves the task to `.tasks/stuck/` rather than committing broken
work. This gate is deliberately blocking (unlike the `mypy` step) — do not
add a flag or config path that skips it "just this once."
