# Dead Code & Orphaned Module Sweep — 2026-08-06 (Issue #423)

**Result: CLEAN** — No orphaned modules found. No truly dead code detected.

## Checks Performed

### Module-Level Orphan Detection

- **Sweep tool**: `dead_code_sweep.py` (AST-based BFS import discovery)
- **Modules scanned**: 29 (26 top-level + 3 webapp sub-modules)
- **Entry points**: `main.py`, `scheduler.py`, `sync_history.py`, `insight_cron.py`,
  `commit_hygiene.py`, `dead_code_sweep.py`, `webapp/app.py`
- **Orphans found**: 0

### Previously Known Orphans (Resolved)

- **`programme_inference.py`**: Wired into `webapp/app.py` via `_run_hevy_inference()`
  (PR #142). Imported at lines 34 and 1022. ✅ Resolved.
- **`hevy_reader.py`**: Imported by `programme_inference.py` (line 24) and
  `webapp/app.py` (line 1021). ✅ Resolved.
- **`sync_history.py`**: Imported by `main.py` (line 317) and `webapp/app.py`
  (line 1381). Also executable standalone. ✅ Resolved.

### Truly Dead Code Check

- **Stale bytecode**: None found (`clean_stale_pycache()` removed 0 files)
- **Git log analysis**: Shallow clone — full commit history unavailable for
  replacement-detection. AST-based import graph shows all modules reachable.
- **Documentation references**: All modules referenced in AGENTS.md §7 are
  marked resolved.

### Import Graph Verification

All 29 modules reachable from entry points via BFS:

```
Entry: main.py → imports: ai_provider, checkin, config, database, datetime,
  gemini_engine, google_health_client, health_connect, hevy_client,
  hevy_parser, hevy_sync, insights, lifestyle, program, sync_history,
  sys, telegram_notifier

Entry: webapp/app.py → imports: ai_provider, analytics, config, database,
  google_health_auth, hevy_client, hevy_parser, hevy_reader, insights,
  lifestyle, program, programme_inference, sync_history, webapp.ai_widgets,
  webapp.charts

Entry: scheduler.py → imports: database (init_db, get_all_users)

Entry: insight_cron.py → imports: ai_provider, config, database
```

Transitive closure covers all remaining modules (`encryption` via `database`,
`weather` via `gemini_engine`/`hevy_sync`, `hevy_reader` via
`programme_inference`, etc.).

## Verification Gates

- **ruff**: Clean (0 warnings)
- **pytest**: 533 passed, 1 skipped (69 in `test_dead_code_sweep.py`)
- **dead_code_sweep.py**: Reports clean (`{"status":"clean","orphans":[]}`)
- **Manual `grep` audit**: All 26 top-level modules confirmed imported from
  reachable runtime path or registered as entry points.

## Summary

No action required. All modules are properly wired. The sweep tool is
scheduled hourly in `scheduler.py` (`_run_dead_code_sweep()` with
`--create-issues` flag, line 197).
