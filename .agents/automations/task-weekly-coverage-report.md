# Weekly Test Coverage Report

## Objective
Track and close the test-coverage gap called out in `AGENTS.md` §7.

## Instructions
1. Run `python3 -m pytest --cov=. --cov-report=term-missing -q` (install
   `pytest-cov` to `requirements.txt`'s dev section if not already present).
2. Compare this week's coverage percentage per-module against last week's
   (check recent commits for a coverage snapshot; if none exists yet,
   establish the baseline this run and commit it as
   `docs/coverage-baseline.md` or similar for future weeks to diff against).
3. Pick the two lowest-coverage, most product-critical modules (prioritise
   `ai_provider.py`, `gemini_engine.py`, `encryption.py`, and anything
   touched by the multi-tenant migration over cosmetic modules like
   `charts.py`) and add tests for their main code paths.
4. Do not chase 100% coverage on trivial modules (e.g. simple dataclasses)
   at the expense of leaving a genuinely risky module (auth, encryption,
   money-equivalent paths like API-key handling) untested.
