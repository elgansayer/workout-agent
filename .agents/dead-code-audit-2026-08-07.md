# Dead Code & Orphaned Module Sweep — 2026-08-07 (Issue #505)

**Result: CLEAN** — No orphaned modules found. No truly dead code detected.

## Checks Performed

### Module-Level Orphan Detection

- **Sweep tool**: `dead_code_sweep.py` (AST-based BFS import discovery)
- **Modules scanned**: 30 (27 top-level + 3 webapp sub-modules)
- **Entry points**: `main.py`, `scheduler.py`, `sync_history.py`, `insight_cron.py`,
  `commit_hygiene.py`, `dead_code_sweep.py`, `webapp/app.py`
- **Orphans found**: 0

### Manual Import Audit

Every top-level `.py` module confirmed imported from a reachable runtime path:

| Module | Imported by |
|---|---|
| `ai_provider` | `main.py`, `checkin.py`, `gemini_engine.py`, `hevy_sync.py`, `webapp/app.py` |
| `analytics` | `insights.py`, `webapp/app.py` |
| `checkin` | `main.py` |
| `commit_hygiene` | `scheduler.py` (subprocess entry point) |
| `config` | `main.py`, `checkin.py`, `hevy_sync.py`, `webapp/app.py`, `insight_cron.py` |
| `database` | `scheduler.py`, `main.py`, `checkin.py`, `google_health_auth.py`, `google_health_client.py` |
| `dead_code_sweep` | `scheduler.py` (subprocess entry point) |
| `encryption` | `database.py` |
| `gemini_engine` | `main.py`, `checkin.py`, `hevy_sync.py` |
| `google_health_auth` | `webapp/app.py` |
| `google_health_client` | `main.py`, `google_health_auth.py` |
| `health_connect` | `main.py`, `hevy_sync.py` |
| `hevy_client` | `checkin.py`, `hevy_sync.py`, `webapp/app.py`, `sync_history.py`, `hevy_reader.py` |
| `hevy_parser` | `main.py`, `gemini_engine.py`, `database.py`, `webapp/app.py`, `sync_history.py` |
| `hevy_reader` | `programme_inference.py`, `webapp/app.py` |
| `hevy_sync` | `main.py` |
| `insight_cron` | Entry point (invoked by `scheduler.py` via subprocess) |
| `insights` | `main.py`, `gemini_engine.py`, `hevy_sync.py`, `webapp/app.py` |
| `lifestyle` | `main.py`, `webapp/app.py` |
| `main` | Entry point |
| `program` | `main.py`, `checkin.py`, `gemini_engine.py`, `lifestyle.py`, `hevy_sync.py` |
| `programme_inference` | `webapp/app.py` |
| `scheduler` | Entry point |
| `sync_history` | `main.py`, `webapp/app.py` |
| `telegram_notifier` | `main.py` |
| `weather` | `gemini_engine.py`, `hevy_sync.py` |

### Webapp Sub-Modules

| Module | Imported by |
|---|---|
| `webapp.ai_widgets` | `webapp/app.py` |
| `webapp.app` | Entry point (Gunicorn/Uvicorn target) |
| `webapp.charts` | `webapp/app.py` |

### Truly Dead Code Check

- **Stale bytecode**: None found (`clean_stale_pycache()` removed 0 files)
- **Git log analysis**: All modules have reachable import paths. No replacement-keyword matches.
- **Documentation references**: AGENTS.md §7 confirms all previously-known orphans resolved.

### Previously Known Orphans (Resolved — re-confirmed)

- **`programme_inference.py`**: Wired into `webapp/app.py` via `_run_hevy_inference()` (PR #142). ✅
- **`hevy_reader.py`**: Imported by `programme_inference.py` and `webapp/app.py`. ✅
- **`sync_history.py`**: Imported by `main.py` and `webapp/app.py`. Also executable standalone. ✅

## Verification Gates

- **ruff**: Clean (0 warnings)
- **mypy**: Clean (0 issues on 31 source files)
- **pytest**: 533 passed, 1 skipped
- **dead_code_sweep.py**: Reports clean (`{"status":"clean","orphans":[]}`)
- **import sanity**: All modules reachable from entry points via BFS

## Summary

No action required. All 30 modules (27 top-level + 3 webapp) are properly wired.
The sweep tool is scheduled hourly in `scheduler.py` (`_run_dead_code_sweep()` with
`--create-issues` flag).