# Hourly Dead Code & Orphaned Module Sweep

## Objective
Prevent half-finished modules from silently rotting, unreferenced, in the
repo (per `AGENTS.md` §4's "never leave dead orphaned modules" rule).

## Instructions
1. Grep for unused imports/functions ruff's `F401`/`F841` rules already
   catch (covered by the lint-fix automation) — this task is specifically
   about **module-level** orphaning, not line-level dead code.
2. For each top-level `.py` module, confirm it's actually imported from
   somewhere reachable at runtime (`main.py`, `webapp/app.py`, or a module
   those import transitively) — `grep -rn "import <module_name>"` across the
   repo excluding the module's own file and its test file.
3. If you find a module that is defined but never imported anywhere
   (no known examples as of the 2026-08-05 audit — `programme_inference.py`
   and `hevy_reader.py` were previously orphaned but have since been wired
   by PR #142), do not delete it outright in this automation. Instead file a
   `task_add()` entry naming the specific module and what wiring it up would
   require, tagged for the `programme-builder-ui` or relevant skill, so a
   dedicated task handles the wiring deliberately rather than a drive-by
   hourly sweep making that call.
4. If you find truly dead code (no plausible future caller, superseded by
   another module, confirmed via `git log` that it was replaced), remove it
   completely — don't leave commented-out code or `# removed` markers.
