# Hourly Test Watch

## Objective
Catch and fix any drift between the code and the test suite before it
accumulates.

## Instructions
1. Run `python3 -m pytest -q`. If everything passes, check test coverage
   gaps instead: pick one module listed in `AGENTS.md` §7's "zero test
   coverage" list that still has no corresponding `tests/test_*.py` file,
   and add a focused test module for its most important function (mock all
   network/AI calls — no test may hit a real external API, per the
   `connector-integration` skill).
2. If tests are failing, fix the underlying code or the test, whichever is
   actually wrong — do not weaken an assertion just to make it pass. If a
   failure reveals a genuine multi-tenancy or security issue, treat it as
   higher priority than the test itself.
3. Run the `verification-gate` skill's steps before committing.
