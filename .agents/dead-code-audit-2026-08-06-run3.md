# Dead Code & Orphaned Module Sweep — 2026-08-06 Run 3 (Issue #479)

**Result: CLEAN** — No orphaned modules found. No truly dead code detected.

## Checks Performed

### Module-Level Orphan Detection

- **Sweep tool**: `dead_code_sweep.py` (AST-based BFS import discovery + grep fallback)
- **Modules scanned**: 29 (26 top-level + 3 webapp sub-modules)
- **Entry points**: `main.py`, `scheduler.py`, `sync_history.py`, `insight_cron.py`,
  `commit_hygiene.py`, `dead_code_sweep.py`, `webapp/app.py`
- **Orphans found**: 0

### Previously Known Orphans (re-confirmed wired)

- **`programme_inference.py`**: ✅ Wired — imported by `webapp/app.py` via
  `_run_hevy_inference()` (PR #142)
- **`hevy_reader.py`**: ✅ Wired — imported by `webapp/app.py` via
  `_run_hevy_inference()` (PR #142)
- **`sync_history.py`**: ✅ Wired — imported by `main.py` (`--sync-history` flag)
  and `webapp/app.py` (`/api/settings/sync-history` endpoint)

### Truly Dead Code Check

- **`find_truly_dead()`**: 0 candidates — no orphans to evaluate
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
- **mypy**: Clean on all 31 source files
- **Import sanity**: All 26 top-level modules importable
- **dead_code_sweep.py**: Reports clean (`{"status":"clean","orphans":[]}`)

## Summary

No action required. All 29 modules are properly wired. The sweep tool
(PR #423) is scheduled hourly in `scheduler.py` via `_run_dead_code_sweep()`
and continues to operate correctly.
## Summary
- **Sweep tool**: `dead_code_sweep.py` (69 tests, all passing)
- **Result**: CLEAN — 0 orphaned modules detected
- **Exit code**: 0

## Module Verification
All 27 top-level `.py` modules and 3 `webapp/` sub-modules verified as reachable:

| Module | Reachable Via |
|--------|--------------|
| `ai_provider.py` | `main.py`, `checkin.py`, `gemini_engine.py`, `hevy_sync.py`, `webapp/app.py`, `insight_cron.py` |
| `analytics.py` | `insights.py`, `webapp/app.py` |
| `checkin.py` | `main.py` |
| `config.py` | `main.py`, `checkin.py`, `hevy_sync.py`, others |
| `database.py` | `main.py`, `scheduler.py`, `checkin.py`, `webapp/app.py`, all modules |
| `encryption.py` | `database.py` |
| `gemini_engine.py` | `main.py`, `checkin.py`, `hevy_sync.py` |
| `google_health_auth.py` | `webapp/app.py` |
| `google_health_client.py` | `main.py`, `google_health_auth.py` |
| `health_connect.py` | `main.py`, `hevy_sync.py` |
| `hevy_client.py` | `main.py`, `checkin.py`, `hevy_sync.py`, `webapp/app.py` |
| `hevy_parser.py` | `main.py`, `gemini_engine.py`, `database.py` |
| `hevy_reader.py` | `programme_inference.py`, `webapp/app.py` |
| `hevy_sync.py` | `main.py` |
| `insights.py` | `main.py`, `gemini_engine.py`, `hevy_sync.py` |
| `lifestyle.py` | `main.py`, `webapp/app.py` |
| `program.py` | `main.py`, `checkin.py`, `gemini_engine.py` |
| `programme_inference.py` | `webapp/app.py` |
| `sync_history.py` | `main.py`, `webapp/app.py` |
| `telegram_notifier.py` | `main.py` |
| `weather.py` | `gemini_engine.py`, `hevy_sync.py` |
| `webapp/app.py` | Entry point (uvicorn) |
| `webapp/ai_widgets.py` | `webapp/app.py` |
| `webapp/charts.py` | `webapp/app.py` |

Entry points (invoked directly or via subprocess): `main.py`, `scheduler.py`, `sync_history.py`, `insight_cron.py`, `dead_code_sweep.py`, `commit_hygiene.py`

## Verification Gates
- **ruff**: All checks passed (0 warnings)
- **mypy**: No issues found in 61 source files
- **pytest**: 569 passed (2 deprecation warnings from google._upb, not ours)
- **import-sanity**: All modules importable

## Actions Taken
- Updated AGENTS.md §7 audit timestamp to include `hourly dead-code sweep #479 checked 2026-08-06`
- No orphaned modules detected — no issues filed
- No truly-dead code detected — nothing to prune
