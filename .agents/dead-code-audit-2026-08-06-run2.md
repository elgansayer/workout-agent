# Dead Code & Orphaned Module Sweep — 2026-08-06 Run 2 (Issue #445)

**Result: CLEAN** — No orphaned modules found. No truly dead code detected.

## Checks Performed

### Module-Level Orphan Detection

- **Sweep tool**: `dead_code_sweep.py` (AST-based BFS import discovery + grep fallback)
- **Modules scanned**: 29 (26 top-level + 3 webapp sub-modules)
- **Entry points**: `main.py`, `scheduler.py`, `sync_history.py`, `insight_cron.py`,
  `commit_hygiene.py`, `dead_code_sweep.py`, `webapp/app.py`
- **Orphans found**: 0

### Previously Known Orphans

- **`programme_inference.py`**: ✅ Wired — imported by `webapp/app.py` via
  `_run_hevy_inference()` (PR #142)
- **`hevy_reader.py`**: ✅ Wired — imported by `programme_inference.py` and
  `webapp/app.py`
- **`sync_history.py`**: ✅ Wired — imported by `main.py` and `webapp/app.py`

### Truly Dead Code Check

- **Stale bytecode**: None found
- **Git log analysis**: Shallow clone — full commit history unavailable for
  replacement-detection. AST-based import graph shows all modules reachable.

### Webapp Sub-module Verification

- `webapp.ai_widgets`: ✅ Wired via `from webapp import ai_widgets` in `webapp/app.py`
- `webapp.charts`: ✅ Wired via `from webapp import charts` in `webapp/app.py`
- `webapp.app`: ✅ Entry point (web server)

## Verification Gates

- **ruff**: Clean (0 warnings)
- **pytest**: 533 passed, 1 skipped (69 in `test_dead_code_sweep.py`)
- **mypy**: Clean on `dead_code_sweep.py`
- **compileall**: Clean
- **dead_code_sweep.py**: Reports clean (`{"status":"clean","orphans":[]}`)

## Summary

No action required. All 29 modules are properly wired. The sweep tool
(PR #423) is scheduled hourly in `scheduler.py` and continues to operate
correctly.