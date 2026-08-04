# Hourly Type-Check Sweep

## Objective
Incrementally improve type coverage without a disruptive big-bang typing pass.

## Instructions
1. Run `mypy --ignore-missing-imports .` and pick the single file with the
   most errors that hasn't been touched by this automation in the last 24
   hours (check recent commit messages for
   "type-check sweep: <file>" to avoid repeats).
2. Add type hints to fix those errors: parameter/return annotations, `from
   __future__ import annotations`, narrowing `Any` to a concrete type where
   the actual value is knowable.
3. Do not change runtime behaviour to satisfy the type checker — if a real
   bug is exposed (e.g. a function can genuinely receive `None` and doesn't
   handle it), fix the bug, don't just silence the type error with a cast.
4. Commit with message `type-check sweep: <file>` so future runs of this
   automation can tell what's already been swept.
5. Run the `verification-gate` skill's steps before committing.
