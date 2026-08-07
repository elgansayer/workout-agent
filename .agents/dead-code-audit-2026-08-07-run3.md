# Dead Code & Orphaned Module Sweep — 2026-08-07 Run #3 (Issue #552)

**Result: CLEAN** — No orphaned modules found. No truly dead code detected.

## Checks Performed

### Module-Level Orphan Detection

- **Sweep tool**: `dead_code_sweep.py` (AST-based BFS import discovery)
- **Modules scanned**: 31 (27 top-level + 3 webapp sub-modules + 1 scheduled)
- **Entry points**: `main.py`, `scheduler.py`, `sync_history.py`, `insight_cron.py`,
  `commit_hygiene.py`, `dead_code_sweep.py`, `webapp/app.py`
- **Orphans found**: 0

### Manual Import Audit

All top-level `.py` modules confirmed reachable:

| Module | Imported by |
|---|---|
| `ai_provider` | `main.py`, `checkin.py`, `gemini_engine.py`, `hevy_sync.py`, `insight_cron.py`, `webapp/app.py` |
| `analytics` | `insights.py`, `webapp/app.py` |
| `checkin` | `main.py` |
| `commit_hygiene` | `scheduler.py` (subprocess entry point) |
| `config` | `main.py`, `checkin.py`, `hevy_sync.py`, `sync_history.py`, `insight_cron.py` |
| `database` | `scheduler.py`, `main.py`, `checkin.py`, `google_health_auth.py`, `ai_provider.py`, `sync_history.py`, `insight_cron.py`, `hevy_sync.py`, `gemini_engine.py` |
| `dead_code_sweep` | `scheduler.py` (subprocess entry point) |
| `encryption` | `database.py` |
| `gemini_engine` | `main.py`, `checkin.py`, `hevy_sync.py` |
| `google_health_auth` | `webapp/app.py` |
| `google_health_client` | `main.py`, `google_health_auth.py` |
| `health_connect` | `main.py`, `hevy_sync.py` |
| `hevy_client` | `main.py`, `checkin.py`, `hevy_sync.py`, `webapp/app.py`, `sync_history.py`, `hevy_reader.py` |
| `hevy_parser` | `main.py`, `gemini_engine.py`, `database.py`, `webapp/app.py`, `sync_history.py` |
| `hevy_reader` | `programme_inference.py`, `webapp/app.py` |
| `hevy_sync` | `main.py` |
| `insight_cron` | Entry point (invoked by `scheduler.py` via subprocess) |
| `insights` | `main.py`, `gemini_engine.py`, `hevy_sync.py`, `webapp/app.py` |
| `lifestyle` | `main.py`, `webapp/app.py` |
| `main` | Entry point |
| `program` | `main.py`, `checkin.py`, `database.py`, `lifestyle.py` |
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

- **Stale bytecode**: None
- **Git log analysis**: All modules have active import paths. No replacement-keyword matches.
- **Previously-known orphans verify**: `programme_inference.py`, `hevy_reader.py`, `sync_history.py` all remain properly wired.

## Verification Gates

- **ruff**: Clean (0 warnings)
- **pytest**: 575 passed
- **mypy**: Clean (0 issues on 31 source files)
- **dead_code_sweep.py**: Reports clean (`{"status":"clean","orphans":[]}`)
- **import sanity**: `import webapp.app; import main` OK

## Summary

No action required. All modules are properly wired and the sweep tool runs hourly
in `scheduler.py` (`_run_dead_code_sweep()` with `--create-issues` flag).