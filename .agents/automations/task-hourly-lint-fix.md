# Hourly Lint & Format Fix

## Objective
Keep the codebase clean and consistently formatted every hour.

## Instructions
1. Run `ruff check --fix .` and `ruff format .` from the repo root.
2. Manually resolve any lint errors ruff couldn't auto-fix (unused imports
   left behind by a partial refactor, genuine logic issues it flags).
3. Confirm no new `mypy` errors were introduced in any file touched by the
   auto-fix (advisory only — see the `verification-gate` skill — but don't
   let auto-fix silently make typing worse).
4. Run the `verification-gate` skill's steps before committing.
