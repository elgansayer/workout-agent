# Test coverage baseline

This is the first repository coverage snapshot for weekly comparisons. No earlier
coverage baseline was present in the current tree, so issue #735 establishes the
baseline rather than claiming a week-over-week repository-wide delta.

## Snapshot

- Date: 2026-09-18
- Base revision: `cf55e30f3565927d764165b21decb3327b10698a` (`main`)
- Python: 3.13.5
- pytest: 8.4.2
- pytest-cov: 7.1.0
- coverage.py: 7.16.1
- Result: 857 passed, 36 skipped, 63 warnings
- Overall: 86% (15,818 statements; 2,198 missed)

Run from the repository root:

```console
python3 -m pytest --cov=. --cov-report=term-missing -q
```

`pytest-cov` was loaded from an isolated temporary target for this factory run
because the shared virtual environment was read-only. It is now declared in the
development requirements, so a clean environment can run the command directly.

## Critical-module change in this run

| Module | Before | Current | Delta | Missed lines before/current |
| --- | ---: | ---: | ---: | ---: |
| `backend/ai_provider.py` | 65% | 94% | +29 pp | 44 / 7 |
| `backend/encryption.py` | 86% | 91% | +5 pp | 5 / 3 |

`ai_provider.py` and `encryption.py` were selected because they were the two
lowest-covered modules in the issue's explicit product-critical priority list.
`gemini_engine.py` already measured 97%. The new provider tests exercise request
construction, synchronous and streaming responses, empty chunks, provider names,
the DeepSeek base URL, and settings metadata. The encryption test exercises the
configured-key/missing-dependency branch and verifies that the secret is not
written to logs. The production encryption fallback now also warns when no
encryption key is configured, making plaintext credential storage visible to
operators while preserving the documented local-development compatibility path.

## Production-module baseline

Test modules are excluded from this table. The overall figure above intentionally
matches the issue's exact `--cov=.` command and therefore includes collected test
files; use the per-module rows below for future product-code comparisons.

| Module | Statements | Missed | Coverage |
| --- | ---: | ---: | ---: |
| `backend/adaptive_audit.py` | 13 | 1 | 92% |
| `backend/adaptive_training.py` | 48 | 6 | 88% |
| `backend/ai_provider.py` | 127 | 7 | 94% |
| `backend/analytics.py` | 59 | 3 | 95% |
| `backend/checkin.py` | 156 | 50 | 68% |
| `backend/commit_hygiene.py` | 168 | 8 | 95% |
| `backend/config.py` | 62 | 2 | 97% |
| `backend/conftest.py` | 6 | 0 | 100% |
| `backend/connector_health.py` | 234 | 31 | 87% |
| `backend/connectors/__init__.py` | 3 | 0 | 100% |
| `backend/connectors/base.py` | 81 | 7 | 91% |
| `backend/connectors/builtin.py` | 9 | 0 | 100% |
| `backend/connectors/conformance.py` | 18 | 0 | 100% |
| `backend/connectors/errors.py` | 12 | 1 | 92% |
| `backend/connectors/fitbit.py` | 27 | 3 | 89% |
| `backend/connectors/fixtures.py` | 13 | 2 | 85% |
| `backend/connectors/garmin.py` | 22 | 4 | 82% |
| `backend/connectors/health_connect.py` | 15 | 4 | 73% |
| `backend/connectors/health_connect_ingest.py` | 27 | 2 | 93% |
| `backend/connectors/oauth.py` | 28 | 3 | 89% |
| `backend/connectors/oura.py` | 19 | 5 | 74% |
| `backend/connectors/permissions.py` | 9 | 0 | 100% |
| `backend/connectors/polar.py` | 19 | 5 | 74% |
| `backend/connectors/provider.py` | 11 | 1 | 91% |
| `backend/connectors/registry.py` | 26 | 4 | 85% |
| `backend/connectors/simulator.py` | 20 | 1 | 95% |
| `backend/connectors/status.py` | 12 | 1 | 92% |
| `backend/connectors/withings.py` | 19 | 5 | 74% |
| `backend/data_classification.py` | 95 | 10 | 89% |
| `backend/database.py` | 568 | 81 | 86% |
| `backend/dynamic_programme.py` | 228 | 27 | 88% |
| `backend/encryption.py` | 35 | 3 | 91% |
| `backend/gemini_engine.py` | 107 | 3 | 97% |
| `backend/google_health_auth.py` | 100 | 61 | 39% |
| `backend/google_health_client.py` | 100 | 16 | 84% |
| `backend/health_activity_reconcile.py` | 28 | 1 | 96% |
| `backend/health_adapter_checks.py` | 15 | 4 | 73% |
| `backend/health_api_contract.py` | 3 | 0 | 100% |
| `backend/health_api_models.py` | 14 | 0 | 100% |
| `backend/health_audit_event.py` | 13 | 1 | 92% |
| `backend/health_backfill.py` | 13 | 1 | 92% |
| `backend/health_backfill_state.py` | 14 | 1 | 93% |
| `backend/health_cache_policy.py` | 2 | 0 | 100% |
| `backend/health_capability_state.py` | 11 | 0 | 100% |
| `backend/health_checkin.py` | 14 | 1 | 93% |
| `backend/health_coaching_context.py` | 7 | 0 | 100% |
| `backend/health_companion.py` | 15 | 0 | 100% |
| `backend/health_conflicts.py` | 18 | 2 | 89% |
| `backend/health_connect.py` | 29 | 0 | 100% |
| `backend/health_connection.py` | 22 | 0 | 100% |
| `backend/health_connection_errors.py` | 9 | 0 | 100% |
| `backend/health_connector_config.py` | 18 | 2 | 89% |
| `backend/health_consent.py` | 17 | 1 | 94% |
| `backend/health_contract_fixture.py` | 14 | 0 | 100% |
| `backend/health_cursor.py` | 11 | 0 | 100% |
| `backend/health_daily_pipeline.py` | 20 | 0 | 100% |
| `backend/health_data_quality.py` | 15 | 0 | 100% |
| `backend/health_dedup.py` | 34 | 3 | 91% |
| `backend/health_delete.py` | 6 | 1 | 83% |
| `backend/health_disconnect.py` | 8 | 0 | 100% |
| `backend/health_export.py` | 15 | 1 | 93% |
| `backend/health_feature_flags.py` | 11 | 2 | 82% |
| `backend/health_freshness.py` | 17 | 0 | 100% |
| `backend/health_ingestion.py` | 21 | 0 | 100% |
| `backend/health_legacy_bridge.py` | 11 | 0 | 100% |
| `backend/health_limits.py` | 14 | 2 | 86% |
| `backend/health_metric_validation.py` | 8 | 1 | 88% |
| `backend/health_migration.py` | 6 | 0 | 100% |
| `backend/health_migration_status.py` | 11 | 1 | 91% |
| `backend/health_models.py` | 88 | 6 | 93% |
| `backend/health_normalization.py` | 8 | 0 | 100% |
| `backend/health_notification_context.py` | 11 | 0 | 100% |
| `backend/health_observability.py` | 16 | 2 | 88% |
| `backend/health_outliers.py` | 9 | 1 | 89% |
| `backend/health_payload.py` | 11 | 0 | 100% |
| `backend/health_provenance_view.py` | 12 | 0 | 100% |
| `backend/health_provider_catalog.py` | 14 | 0 | 100% |
| `backend/health_provider_contracts.py` | 7 | 2 | 71% |
| `backend/health_provider_identity.py` | 10 | 0 | 100% |
| `backend/health_provider_matrix.py` | 2 | 0 | 100% |
| `backend/health_provider_scores.py` | 14 | 1 | 93% |
| `backend/health_quota.py` | 11 | 0 | 100% |
| `backend/health_readiness.py` | 34 | 3 | 91% |
| `backend/health_replay.py` | 11 | 1 | 91% |
| `backend/health_repository.py` | 6 | 0 | 100% |
| `backend/health_retention.py` | 19 | 1 | 95% |
| `backend/health_schedule.py` | 6 | 0 | 100% |
| `backend/health_schema.py` | 1 | 0 | 100% |
| `backend/health_secret_redaction.py` | 11 | 1 | 91% |
| `backend/health_service.py` | 13 | 0 | 100% |
| `backend/health_source_explanation.py` | 7 | 1 | 86% |
| `backend/health_source_policy.py` | 13 | 1 | 92% |
| `backend/health_sqlite_repository.py` | 27 | 6 | 78% |
| `backend/health_status_summary.py` | 13 | 0 | 100% |
| `backend/health_summary.py` | 23 | 2 | 91% |
| `backend/health_sync.py` | 30 | 1 | 97% |
| `backend/health_sync_lock.py` | 5 | 1 | 80% |
| `backend/health_sync_orchestrator.py` | 14 | 0 | 100% |
| `backend/health_sync_plan.py` | 12 | 0 | 100% |
| `backend/health_sync_progress.py` | 11 | 1 | 91% |
| `backend/health_sync_security.py` | 6 | 0 | 100% |
| `backend/health_test_action.py` | 13 | 0 | 100% |
| `backend/health_time.py` | 8 | 0 | 100% |
| `backend/health_units.py` | 5 | 0 | 100% |
| `backend/health_user_preferences.py` | 13 | 1 | 92% |
| `backend/health_write_guard.py` | 7 | 0 | 100% |
| `backend/hevy_client.py` | 198 | 25 | 87% |
| `backend/hevy_parser.py` | 116 | 12 | 90% |
| `backend/hevy_reader.py` | 165 | 2 | 99% |
| `backend/hevy_sync.py` | 200 | 117 | 42% |
| `backend/insight_cron.py` | 86 | 22 | 74% |
| `backend/insights.py` | 226 | 24 | 89% |
| `backend/lifestyle.py` | 65 | 2 | 97% |
| `backend/main.py` | 180 | 38 | 79% |
| `backend/program.py` | 90 | 6 | 93% |
| `backend/programme_inference.py` | 143 | 8 | 94% |
| `backend/push_notifier.py` | 28 | 11 | 61% |
| `backend/recovery.py` | 40 | 1 | 98% |
| `backend/release_gate.py` | 103 | 28 | 73% |
| `backend/scheduler.py` | 196 | 117 | 40% |
| `backend/scripts/__init__.py` | 0 | 0 | 100% |
| `backend/scripts/check_data_policy.py` | 155 | 53 | 66% |
| `backend/scripts/check_security_headers.py` | 85 | 45 | 47% |
| `backend/scripts/repository_inventory.py` | 345 | 87 | 75% |
| `backend/scripts/sync_history.py` | 60 | 10 | 83% |
| `backend/training_export.py` | 16 | 1 | 94% |
| `backend/weather.py` | 33 | 0 | 100% |
| `backend/webapp/__init__.py` | 14 | 2 | 86% |
| `backend/webapp/ai_widgets.py` | 103 | 5 | 95% |
| `backend/webapp/app.py` | 788 | 444 | 44% |
| `backend/webapp/auth_boundary.py` | 72 | 4 | 94% |
| `backend/webapp/cache_security.py` | 84 | 5 | 94% |
| `backend/webapp/charts.py` | 150 | 4 | 97% |
| `backend/webapp/csrf_security.py` | 152 | 14 | 91% |
| `backend/webapp/health.py` | 94 | 7 | 93% |
| `backend/webapp/mutation_security.py` | 182 | 21 | 88% |
| `backend/webapp/proxy_security.py` | 194 | 48 | 75% |
| `backend/webapp/runtime_security.py` | 50 | 0 | 100% |
| `backend/webapp/security_headers.py` | 100 | 6 | 94% |
| `tools/backup_restore.py` | 415 | 184 | 56% |
| `tools/check_actions_pinning.py` | 107 | 33 | 69% |
| `tools/check_openhands_control_plane.py` | 59 | 21 | 64% |
| `tools/ops_drill.py` | 172 | 51 | 70% |
| `tools/supply_chain.py` | 122 | 43 | 65% |
| `tools/validate_data_flow_inventory.py` | 140 | 36 | 74% |

## Next weekly comparison

Run the same command with the same coverage major version, compare modules by path,
and record percentage-point and missed-line deltas. Investigate deleted or renamed
modules explicitly rather than treating their disappearance as an improvement.
Continue prioritising authentication, tenant isolation, credentials, connectors,
and programme/persistence paths over cosmetic or trivial modules.
