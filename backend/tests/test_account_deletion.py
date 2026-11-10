"""Deterministic tests for the account and health-data deletion workflow."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import account_deletion as ad
import pytest
from database import get_all_users, get_or_create_user, init_db
from health_delete import HEALTH_USER_TABLES
from health_migration import migrate_health_schema
from webapp.csrf_security import CSRFTokenStore

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
AFTER_WINDOW = NOW + timedelta(days=8)
EMAIL_A = "athlete.a@example.com"
EMAIL_B = "athlete.b@example.com"


@pytest.fixture(autouse=True)
def _default_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACCOUNT_DELETION_GRACE_DAYS", raising=False)
    monkeypatch.delenv("ACCOUNT_TOMBSTONE_RETENTION_DAYS", raising=False)


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "data" / "workout.db")
    init_db(path)
    with sqlite3.connect(path) as conn:
        migrate_health_schema(conn)
    CSRFTokenStore(path)._connect().close()  # lazily-created cache table
    return path


@pytest.fixture
def users(db: str) -> tuple[str, str]:
    a = get_or_create_user(EMAIL_A, "Athlete A", db_path=db)["id"]
    b = get_or_create_user(EMAIL_B, "Athlete B", db_path=db)["id"]
    _seed(db, a)
    _seed(db, b)
    return a, b


def _seed(db: str, user_id: str) -> None:
    """Give *user_id* synthetic rows in every user-owned store."""
    stamp = "2026-01-01T00:00:00+00:00"
    with sqlite3.connect(db) as conn:
        run = conn.execute
        run(
            "INSERT INTO workout_history (date, hevy_payload, user_id) VALUES (?, ?, ?)",
            ("2026-01-01", "{}", user_id),
        )
        run(
            "INSERT INTO programme_state (user_id, current_day, split_name) VALUES (?, 1, 'Synthetic')",
            (user_id,),
        )
        run(
            "INSERT INTO exercise_progress (date, exercise_name, top_weight_kg, top_reps, sets, user_id) "
            "VALUES ('2026-01-01', 'Synthetic Squat', 100, 5, 3, ?)",
            (user_id,),
        )
        run(
            "INSERT INTO hevy_routines (user_id, routine_key, routine_id, content_hash) VALUES (?, 'k', 'r', 'h')",
            (user_id,),
        )
        for key in (
            "google_health_refresh_token",
            "google_health_oauth_state",
            "checkin_number",
            "routine_folder_id",
        ):
            run(
                "INSERT INTO hevy_meta (user_id, key, value) VALUES (?, ?, 'synthetic')",
                (user_id, key),
            )
        run(
            "INSERT INTO check_ins (number, date, workouts_done, weeks, message, user_id) "
            "VALUES (1, '2026-01-01', 3, 4, 'Synthetic check-in', ?)",
            (user_id,),
        )
        run(
            "INSERT INTO daily_log (date, day, focus, carb_tier, plan, lifestyle, user_id) "
            "VALUES ('2026-01-01', 1, 'Legs', 'high', 'plan', 'life', ?)",
            (user_id,),
        )
        run(
            "INSERT INTO body_metrics (date, weight_kg, user_id) VALUES ('2026-01-01', 80, ?)",
            (user_id,),
        )
        run(
            "INSERT INTO dashboard_insights (user_id, date, insight_json) VALUES (?, '2026-01-01', '{}')",
            (user_id,),
        )
        run(
            "INSERT INTO deep_correlations (user_id, date, insight_markdown) VALUES (?, '2026-01-01', 'md')",
            (user_id,),
        )
        run(
            "INSERT INTO reasoning_logs (user_id, context_id, date, exercise_name, reasoning) "
            "VALUES (?, 'ctx', '2026-01-01', 'Synthetic Squat', 'why')",
            (user_id,),
        )
        run(
            "INSERT INTO chat_messages (role, content, created_at, user_id) VALUES ('user', 'hi', ?, ?)",
            (stamp, user_id),
        )
        for provider in ("hevy", "claude", "garmin"):
            run(
                "INSERT INTO user_api_keys (user_id, provider, api_key, created_at, updated_at) "
                "VALUES (?, ?, 'synthetic-key', ?, ?)",
                (user_id, provider, stamp, stamp),
            )
        run(
            "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, created_at) "
            "VALUES (?, 'https://push.example.invalid/x', 'p', 'a', ?)",
            (user_id, stamp),
        )
        run(
            "INSERT INTO notifications (user_id, type, title, message, created_at) "
            "VALUES (?, 'system', 'T', 'M', ?)",
            (user_id, stamp),
        )
        run(
            "INSERT INTO user_preferences (user_id, goals, updated_at) VALUES (?, '[]', ?)",
            (user_id, stamp),
        )
        programme_id = run(
            "INSERT INTO programmes (user_id, source, template_key, definition, active, created_at, updated_at) "
            "VALUES (?, 'template', 'synthetic', '{}', 1, ?, ?)",
            (user_id, stamp, stamp),
        ).lastrowid
        revision_id = run(
            "INSERT INTO programme_revisions "
            "(user_id, programme_id, revision_number, reason, engine_version, created_at) "
            "VALUES (?, ?, 1, 'synthetic', 'test', ?)",
            (user_id, programme_id, stamp),
        ).lastrowid
        run(
            "INSERT INTO programme_routines "
            "(user_id, programme_id, revision_id, position, source_routine_id, title, snapshot_hash, created_at) "
            "VALUES (?, ?, ?, 1, 'synthetic-routine', 'Synthetic', 'hash', ?)",
            (user_id, programme_id, revision_id, stamp),
        )
        run(
            "INSERT INTO programme_blocks "
            "(user_id, programme_id, revision_id, position, key, name, start_week, end_week, created_at) "
            "VALUES (?, ?, ?, 1, 'block', 'Synthetic', 1, 1, ?)",
            (user_id, programme_id, revision_id, stamp),
        )
        run(
            "INSERT INTO exercise_prescriptions "
            "(user_id, programme_id, revision_id, exercise_template_id, exercise_title, "
            "role, progression_method, rule_version, created_at) "
            "VALUES (?, ?, ?, 'template', 'Synthetic Squat', 'primary', 'linear', 'test', ?)",
            (user_id, programme_id, revision_id, stamp),
        )
        run(
            "INSERT INTO programme_week_states "
            "(user_id, programme_id, revision_id, week_number, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (user_id, programme_id, revision_id, stamp, stamp),
        )
        run(
            "INSERT INTO adaptation_decisions "
            "(user_id, programme_id, to_revision_id, rule_version, created_at) "
            "VALUES (?, ?, ?, 'test', ?)",
            (user_id, programme_id, revision_id, stamp),
        )
        run(
            "INSERT INTO programme_decisions "
            "(user_id, programme_id, source, decision, reason, created_at) "
            "VALUES (?, ?, 'synthetic', 'keep', 'synthetic', ?)",
            (user_id, programme_id, stamp),
        )
        run(
            "INSERT INTO training_rules (user_id, version, rules_json, updated_at) "
            "VALUES (?, 1, '{}', ?)",
            (user_id, stamp),
        )
        run(
            "INSERT INTO hevy_routine_snapshots "
            "(user_id, snapshot_id, routine_id, title, content_hash, schema_version, snapshot_json) "
            "VALUES (?, 'snapshot', 'routine', 'Synthetic', 'hash', 1, '{}')",
            (user_id,),
        )
        run(
            "INSERT INTO routine_workout_matches "
            "(user_id, programme_id, routine_position, source_routine_id, workout_id, "
            "features_json, method, matcher_version, matched_at, created_at) "
            "VALUES (?, ?, 1, 'routine', 'workout', '{}', 'synthetic', 'test', ?, ?)",
            (user_id, programme_id, stamp, stamp),
        )
        run(
            "INSERT INTO routine_match_overrides "
            "(user_id, pattern_type, pattern_value, routine_id, routine_snapshot_id, created_at) "
            "VALUES (?, 'routine', 'pattern', 'routine', 'snapshot', ?)",
            (user_id, stamp),
        )
        run(
            "INSERT INTO exercise_history_baselines "
            "(user_id, template_id, rule_version, confidence, baseline_json, created_at) "
            "VALUES (?, 'template', 'test', 'high', '{}', ?)",
            (user_id, stamp),
        )
        run(
            "INSERT INTO hevy_connections "
            "(user_id, provider, state, capabilities_json, tested_at, created_at, updated_at) "
            "VALUES (?, 'hevy', 'connected', '{}', ?, ?, ?)",
            (user_id, stamp, stamp, stamp),
        )
        run(
            "INSERT INTO user_identities "
            "(user_id, provider, subject, email, email_verified, created_at) "
            "VALUES (?, 'google', ?, ?, 1, ?)",
            (user_id, f"subject-{user_id}", f"identity-{user_id}@example.test", stamp),
        )
        run(
            "INSERT INTO user_sessions "
            "(session_token_hash, user_id, created_at, last_seen_at) "
            "VALUES (?, ?, 1, 1)",
            (f"session-{user_id}", user_id),
        )
        run(
            "INSERT INTO csrf_tokens (token_hash, session_hash, user_id, expires_at) "
            "VALUES (?, 's', ?, 9999999999)",
            (f"token-{user_id}", user_id),
        )
        run(
            "INSERT INTO health_connections (id, user_id, provider, state) "
            "VALUES (?, ?, 'oura', 'connected')",
            (f"conn-{user_id}", user_id),
        )
        run(
            "INSERT INTO health_metrics (user_id, metric_type, value, unit, observed_at, provider, "
            "connection_id, fingerprint) VALUES (?, 'weight_kg', 80, 'kg', ?, 'oura', 'c', ?)",
            (user_id, stamp, f"fp-{user_id}"),
        )
        run(
            "INSERT INTO health_sync_runs (user_id, provider, connection_id, state, started_at) "
            "VALUES (?, 'oura', 'c', 'ok', ?)",
            (user_id, stamp),
        )


def _user_tables(conn: sqlite3.Connection) -> list[str]:
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return [
        name
        for name in names
        if "user_id"
        in {col[1] for col in conn.execute(f"PRAGMA table_info({ad._quote(name)})")}
    ]


def _row_counts(db: str, user_id: str) -> dict[str, int]:
    """Rows held for *user_id* in every table (found by schema, not by manifest)."""
    counts: dict[str, int] = {}
    with sqlite3.connect(db) as conn:
        for table in _user_tables(conn):
            if table in ad.RETAINED_LEDGER_TABLES:
                continue
            found = conn.execute(
                f"SELECT COUNT(*) FROM {ad._quote(table)} WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            if found:
                counts[table] = found
    return counts


def _scalar(db: str, sql: str, *params: Any) -> Any:
    with sqlite3.connect(db) as conn:
        row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def _close_account(db: str, user_id: str, *, email: str) -> ad.DeletionStatus:
    request = ad.request_deletion(db, user_id, "account", email, now=NOW)
    status = ad.execute_deletion(db, request["request_id"], now=AFTER_WINDOW)
    assert status is not None
    return status


# ---------------------------------------------------------------------------
# Inventory coverage: a new user-owned table cannot silently escape deletion
# ---------------------------------------------------------------------------


def test_every_user_owned_table_is_registered_and_seeded(
    db: str, users: tuple[str, str]
) -> None:
    registered = {store.table for store in ad.USER_DATA_STORES}
    with sqlite3.connect(db) as conn:
        discovered = set(_user_tables(conn)) - set(ad.RETAINED_LEDGER_TABLES)
        assert ad._unregistered_user_tables(conn) == []
    assert discovered == registered
    # The seed helper must exercise every store, or the erasure tests prove nothing.
    assert set(_row_counts(db, users[0])) == registered
    assert set(HEALTH_USER_TABLES) <= registered


def test_every_store_belongs_to_a_plan_step() -> None:
    account_steps = set(ad.PLANS[ad.DeletionScope.ACCOUNT])
    health_steps = set(ad.PLANS[ad.DeletionScope.HEALTH_DATA])
    for store in ad.USER_DATA_STORES:
        assert store.step in account_steps, store.table
        if store.health:
            assert store.step in health_steps, store.table


def test_ledger_tables_hold_no_personal_columns(db: str) -> None:
    with sqlite3.connect(db) as conn:
        for table in ad.RETAINED_LEDGER_TABLES:
            columns = {col[1] for col in conn.execute(f'PRAGMA table_info("{table}")')}
            assert not columns & {
                "email",
                "display_name",
                "name",
                "ip",
                "payload",
                "message",
            }


def test_no_unregistered_search_index_exists(db: str) -> None:
    with sqlite3.connect(db) as conn:
        assert ad._unregistered_search_indexes(conn) == []
        conn.execute("CREATE VIRTUAL TABLE chat_search USING fts5(content)")
        assert ad._unregistered_search_indexes(conn) == ["chat_search"]


# ---------------------------------------------------------------------------
# Account closure
# ---------------------------------------------------------------------------


def test_account_closure_erases_everything_for_that_user_only(
    db: str, users: tuple[str, str]
) -> None:
    a, b = users
    before_b = _row_counts(db, b)

    status = _close_account(db, a, email=EMAIL_A)

    assert status["state"] == "completed"
    assert status["progress_percent"] == 100
    assert _row_counts(db, a) == {}
    assert _row_counts(db, b) == before_b
    assert _scalar(db, "SELECT COUNT(*) FROM users WHERE id = ?", a) == 0
    assert _scalar(db, "SELECT COUNT(*) FROM users WHERE id = ?", b) == 1


def test_closure_writes_a_personal_data_free_tombstone(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    status = _close_account(db, a, email=EMAIL_A)

    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT * FROM account_tombstones").fetchall()
        ledger = conn.execute("SELECT * FROM account_deletion_requests").fetchall()
        steps = conn.execute("SELECT * FROM account_deletion_steps").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == a
    assert rows[0][1] == status["request_id"]
    assert rows[0][4] == ad.POLICY_VERSION
    dump = repr((rows, ledger, steps)).lower()
    for personal in (EMAIL_A, "athlete a", "synthetic"):
        assert personal not in dump
    assert ad.account_gate(db, a) is ad.AccountGate.CLOSED


def test_tombstone_retention_is_bounded_and_prunable(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    _close_account(db, a, email=EMAIL_A)
    retain_until = datetime.fromisoformat(
        _scalar(db, "SELECT retain_until FROM account_tombstones WHERE user_id = ?", a)
    )
    deleted_at = datetime.fromisoformat(
        _scalar(db, "SELECT deleted_at FROM account_tombstones WHERE user_id = ?", a)
    )
    assert retain_until - deleted_at == timedelta(
        days=ad.DEFAULT_TOMBSTONE_RETENTION_DAYS
    )

    assert ad.prune_expired_tombstones(db, now=retain_until - timedelta(seconds=1)) == 0
    assert _scalar(db, "SELECT COUNT(*) FROM account_tombstones") == 1

    assert ad.prune_expired_tombstones(db, now=retain_until) == 1
    for table in (
        "account_deletion_requests",
        "account_deletion_steps",
        "account_tombstones",
    ):
        assert _scalar(db, f'SELECT COUNT(*) FROM "{table}"') == 0


def test_closed_account_email_can_register_again_as_a_new_account(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    _close_account(db, a, email=EMAIL_A)

    again = get_or_create_user(EMAIL_A, "Athlete A", db_path=db)

    assert again["id"] != a
    assert ad.account_gate(db, again["id"]) is ad.AccountGate.OPEN
    assert ad.account_gate(db, a) is ad.AccountGate.CLOSED


def test_closing_the_account_drops_earlier_ledger_rows_for_that_user(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    earlier = ad.request_deletion(db, a, "health_data", ad.HEALTH_CONFIRMATION, now=NOW)
    ad.execute_deletion(db, earlier["request_id"], now=NOW)
    final = _close_account(db, a, email=EMAIL_A)

    ids = [
        row[0]
        for row in sqlite3.connect(db).execute(
            "SELECT id FROM account_deletion_requests WHERE user_id = ?", (a,)
        )
    ]
    assert ids == [final["request_id"]]
    assert (
        _scalar(
            db,
            "SELECT COUNT(*) FROM account_deletion_steps WHERE request_id = ?",
            earlier["request_id"],
        )
        == 0
    )


def test_execution_is_idempotent(db: str, users: tuple[str, str]) -> None:
    a, _ = users
    status = _close_account(db, a, email=EMAIL_A)

    again = ad.execute_deletion(db, status["request_id"], now=AFTER_WINDOW)

    assert again is not None
    assert again["state"] == "completed"
    assert again["steps"] == status["steps"]


def test_evidence_records_per_step_counts_and_verified_zero_remaining(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    status = _close_account(db, a, email=EMAIL_A)

    by_step = {step["step"]: step for step in status["steps"]}
    assert [step["step"] for step in status["steps"]] == [
        step.value for step in ad.PLANS[ad.DeletionScope.ACCOUNT]
    ]
    assert all(step["state"] == "done" for step in status["steps"])
    assert all(step["remaining_after"] == 0 for step in status["steps"])
    assert (
        by_step["notifications"]["rows_affected"] == 2
    )  # notification + push subscription
    assert by_step["connector_tokens"]["rows_affected"] >= 6
    assert by_step["derived_insights"]["rows_affected"] == 5
    assert by_step["account_identity"]["rows_affected"] == 1


def test_audit_log_carries_the_receipt_but_no_account_identifier(
    db: str, users: tuple[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    a, _ = users
    caplog.set_level(logging.INFO, logger="account_deletion")
    status = _close_account(db, a, email=EMAIL_A)

    lines = [record.getMessage() for record in caplog.records]
    assert any(
        status["request_id"] in line and "event=completed" in line for line in lines
    )
    assert any("event=requested" in line for line in lines)
    joined = "\n".join(lines)
    assert a not in joined
    assert EMAIL_A not in joined


# ---------------------------------------------------------------------------
# Health-data scope
# ---------------------------------------------------------------------------


def test_health_scope_erases_health_and_derived_data_but_keeps_the_account(
    db: str, users: tuple[str, str]
) -> None:
    a, b = users
    before_b = _row_counts(db, b)

    request = ad.request_deletion(db, a, "health_data", "delete   health DATA", now=NOW)
    assert request["execute_after"] == request["requested_at"]
    status = ad.execute_deletion(db, request["request_id"], now=NOW)

    assert status is not None and status["state"] == "completed"
    remaining = _row_counts(db, a)
    for table in (
        "body_metrics",
        "dashboard_insights",
        "deep_correlations",
        "reasoning_logs",
        "chat_messages",
        "check_ins",
        "health_metrics",
        "health_sync_runs",
        "health_connections",
    ):
        assert table not in remaining, table
    # Mixed tables lose only their health rows.
    with sqlite3.connect(db) as conn:
        keys = {
            row[0]
            for row in conn.execute("SELECT key FROM hevy_meta WHERE user_id = ?", (a,))
        }
        providers = {
            row[0]
            for row in conn.execute(
                "SELECT provider FROM user_api_keys WHERE user_id = ?", (a,)
            )
        }
    assert keys == {"checkin_number", "routine_folder_id"}
    assert providers == {"hevy", "claude"}
    # Non-health records, notifications and the account itself remain.
    for table in (
        "workout_history",
        "programmes",
        "notifications",
        "push_subscriptions",
    ):
        assert remaining[table] >= 1, table
    assert _scalar(db, "SELECT COUNT(*) FROM users WHERE id = ?", a) == 1
    assert ad.account_gate(db, a) is ad.AccountGate.OPEN
    assert _row_counts(db, b) == before_b
    assert _scalar(db, "SELECT COUNT(*) FROM account_tombstones") == 0


def test_health_scope_requires_its_own_phrase_not_the_account_email(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    with pytest.raises(ad.DeletionError) as excinfo:
        ad.request_deletion(db, a, "health_data", EMAIL_A, now=NOW)
    assert excinfo.value.code == "confirmation_mismatch"


# ---------------------------------------------------------------------------
# Confirmation, eligibility and the cancellation window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("typed", ["", "delete", "athlete.b@example.com", "yes please"])
def test_wrong_confirmation_creates_nothing(
    db: str, users: tuple[str, str], typed: str
) -> None:
    a, _ = users
    with pytest.raises(ad.DeletionError) as excinfo:
        ad.request_deletion(db, a, "account", typed, now=NOW)
    assert excinfo.value.code == "confirmation_mismatch"
    assert _scalar(db, "SELECT COUNT(*) FROM account_deletion_requests") == 0
    assert ad.account_gate(db, a) is ad.AccountGate.OPEN


def test_confirmation_ignores_case_and_surrounding_whitespace(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    status = ad.request_deletion(db, a, "account", f"  {EMAIL_A.upper()} ", now=NOW)
    assert status["state"] == "pending"


def test_unknown_scope_is_rejected(db: str, users: tuple[str, str]) -> None:
    with pytest.raises(ad.DeletionError) as excinfo:
        ad.request_deletion(db, users[0], "everything", EMAIL_A, now=NOW)
    assert excinfo.value.code == "invalid_scope"


def test_shared_legacy_tenant_and_unknown_accounts_are_not_eligible(db: str) -> None:
    legacy = _scalar(db, "SELECT id FROM users WHERE email = 'legacy@local'")
    for subject in (legacy, "no-such-user", ""):
        with pytest.raises(ad.DeletionError) as excinfo:
            ad.request_deletion(db, subject, "account", "legacy@local", now=NOW)
        assert excinfo.value.code == "not_eligible"
    assert ad.describe(db, legacy)["eligible"] is False


def test_forged_request_for_the_legacy_tenant_fails_without_closing_it(db: str) -> None:
    legacy = _scalar(db, "SELECT id FROM users WHERE email = 'legacy@local'")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO account_deletion_requests (id, user_id, scope, state, requested_at, "
            "execute_after, next_attempt_at) VALUES ('forged', ?, 'account', 'pending', ?, ?, ?)",
            (legacy, ad._ts(NOW), ad._ts(NOW), ad._ts(NOW)),
        )
    status = ad.execute_deletion(db, "forged", now=AFTER_WINDOW)
    assert status is not None
    assert status["state"] == "failed"
    assert status["error_code"] == "not_eligible"
    assert _scalar(db, "SELECT COUNT(*) FROM users WHERE id = ?", legacy) == 1


def test_only_one_open_request_per_account(db: str, users: tuple[str, str]) -> None:
    a, _ = users
    ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
    for scope, confirmation in (
        ("account", EMAIL_A),
        ("health_data", ad.HEALTH_CONFIRMATION),
    ):
        with pytest.raises(ad.DeletionError) as excinfo:
            ad.request_deletion(db, a, scope, confirmation, now=NOW)
        assert excinfo.value.code == "already_requested"


def test_closed_account_cannot_request_again(db: str, users: tuple[str, str]) -> None:
    a, _ = users
    _close_account(db, a, email=EMAIL_A)
    with pytest.raises(ad.DeletionError) as excinfo:
        ad.request_deletion(db, a, "account", EMAIL_A, now=AFTER_WINDOW)
    assert excinfo.value.code == "not_eligible"


def test_nothing_is_erased_before_the_window_ends(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    request = ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
    before = _row_counts(db, a)

    assert request["can_cancel"] is True
    assert request["execute_after"] == ad._ts(NOW + timedelta(days=7))
    status = ad.execute_deletion(db, request["request_id"], now=NOW + timedelta(days=6))

    assert status is not None and status["state"] == "pending"
    assert status["progress_percent"] == 0
    assert _row_counts(db, a) == before
    assert ad.process_due_deletions(db, now=NOW + timedelta(days=6)) == 0
    assert _row_counts(db, a) == before


def test_cancelling_inside_the_window_restores_normal_operation(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    before = _row_counts(db, a)
    request = ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
    assert ad.account_gate(db, a) is ad.AccountGate.PENDING

    cancelled = ad.cancel_deletion(db, a, now=NOW + timedelta(days=1))

    assert cancelled["state"] == "cancelled"
    assert cancelled["steps"] == []
    assert cancelled["can_cancel"] is False
    assert ad.account_gate(db, a) is ad.AccountGate.OPEN
    # A cancelled request is never executed, even long after its due time.
    status = ad.execute_deletion(db, request["request_id"], now=AFTER_WINDOW)
    assert status is not None and status["state"] == "cancelled"
    assert ad.process_due_deletions(db, now=AFTER_WINDOW) == 0
    assert _row_counts(db, a) == before
    # And the account can ask again later.
    assert (
        ad.request_deletion(db, a, "account", EMAIL_A, now=AFTER_WINDOW)["state"]
        == "pending"
    )


def test_cancelling_after_the_window_or_once_running_is_refused(
    db: str, users: tuple[str, str]
) -> None:
    a, b = users
    ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
    with pytest.raises(ad.DeletionError) as late:
        ad.cancel_deletion(db, a, now=AFTER_WINDOW)
    assert late.value.code == "not_cancellable"

    claimed = ad._claim(
        db, _scalar(db, "SELECT id FROM account_deletion_requests"), AFTER_WINDOW
    )
    assert claimed is not None
    with pytest.raises(ad.DeletionError) as running:
        ad.cancel_deletion(db, a, now=AFTER_WINDOW)
    assert running.value.code == "not_cancellable"

    with pytest.raises(ad.DeletionError) as nothing:
        ad.cancel_deletion(db, b, now=NOW)
    assert nothing.value.code == "not_found"


def test_cancel_only_touches_the_callers_own_request(
    db: str, users: tuple[str, str]
) -> None:
    a, b = users
    ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
    with pytest.raises(ad.DeletionError):
        ad.cancel_deletion(db, b, now=NOW)
    assert ad.account_gate(db, a) is ad.AccountGate.PENDING


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 7), ("0", 0), ("14", 14), ("99", 30), ("-3", 0), ("soon", 7)],
)
def test_grace_period_is_configurable_and_bounded(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: int
) -> None:
    if raw is not None:
        monkeypatch.setenv("ACCOUNT_DELETION_GRACE_DAYS", raw)
    assert ad.grace_days() == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 365), ("1", 35), ("90", 90), ("99999", 3650), ("x", 365)],
)
def test_tombstone_retention_is_bounded_below_by_backup_retention(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: int
) -> None:
    if raw is not None:
        monkeypatch.setenv("ACCOUNT_TOMBSTONE_RETENTION_DAYS", raw)
    assert ad.tombstone_retention_days() == expected


def test_zero_day_window_is_due_immediately(
    db: str, users: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_GRACE_DAYS", "0")
    a, _ = users
    request = ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
    assert request["can_cancel"] is False
    status = ad.execute_deletion(db, request["request_id"], now=NOW)
    assert status is not None and status["state"] == "completed"


# ---------------------------------------------------------------------------
# Fencing: scheduled work and the gate
# ---------------------------------------------------------------------------


def test_closing_accounts_are_excluded_from_scheduled_processing(
    db: str, users: tuple[str, str]
) -> None:
    a, b = users

    def scheduled() -> set[str]:
        return {u["id"] for u in get_all_users(db)}

    assert {a, b} <= scheduled()

    ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
    assert a not in scheduled() and b in scheduled()
    assert a in {u["id"] for u in get_all_users(db, include_closing=True)}
    assert ad.is_account_closing(db, a) is True
    assert ad.is_account_closing(db, b) is False

    ad.cancel_deletion(db, a, now=NOW)
    assert a in scheduled()
    assert ad.is_account_closing(db, a) is False


def test_gate_reports_each_stage(db: str, users: tuple[str, str]) -> None:
    a, _ = users
    assert ad.account_gate(db, a) is ad.AccountGate.OPEN
    request = ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
    assert ad.account_gate(db, a) is ad.AccountGate.PENDING
    assert ad._claim(db, request["request_id"], AFTER_WINDOW) is not None
    assert ad.account_gate(db, a) is ad.AccountGate.LOCKED
    ad.execute_deletion(db, request["request_id"], now=AFTER_WINDOW + ad.LEASE * 2)
    assert ad.account_gate(db, a) is ad.AccountGate.CLOSED


def test_gate_without_ledger_tables_is_open_but_other_errors_propagate(
    tmp_path: Path,
) -> None:
    bare = str(tmp_path / "bare.db")
    sqlite3.connect(bare).close()
    assert ad.account_gate(bare, "anyone") is ad.AccountGate.OPEN
    assert ad.is_account_closing(bare, "anyone") is False

    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a sqlite database" * 64)
    with pytest.raises(sqlite3.DatabaseError):
        ad.account_gate(str(garbage), "anyone")


# ---------------------------------------------------------------------------
# Failure paths: verification, retries, crash recovery, late writers
# ---------------------------------------------------------------------------


def test_residual_data_fails_the_request_and_never_closes_the_account(
    db: str, users: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = users
    real_erase = ad._erase_step

    def leaky(
        conn: sqlite3.Connection, db_path: str, user_id: str, scope: Any, step: Any
    ) -> int:
        if step is ad.Step.DERIVED_INSIGHTS:
            return 0  # claims success but leaves rows behind
        return real_erase(conn, db_path, user_id, scope, step)

    monkeypatch.setattr(ad, "_erase_step", leaky)
    request = ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
    status = ad.execute_deletion(db, request["request_id"], now=AFTER_WINDOW)

    assert status is not None
    assert status["state"] == "failed"
    assert status["error_code"] == "residual_data"
    assert [s["state"] for s in status["steps"]][:3] == ["done", "done", "done"]
    assert status["progress_percent"] == 30
    assert _scalar(db, "SELECT COUNT(*) FROM users WHERE id = ?", a) == 1
    assert _scalar(db, "SELECT COUNT(*) FROM account_tombstones") == 0
    assert ad.account_gate(db, a) is ad.AccountGate.LOCKED
    assert ad.is_account_closing(db, a) is True

    # Once the defect is fixed the worker retries after the back-off and finishes.
    monkeypatch.setattr(ad, "_erase_step", real_erase)
    assert ad.process_due_deletions(db, now=AFTER_WINDOW + timedelta(minutes=1)) == 0
    assert ad.process_due_deletions(db, now=AFTER_WINDOW + ad.RETRY_BACKOFF * 2) == 1
    assert _row_counts(db, a) == {}
    assert ad.account_gate(db, a) is ad.AccountGate.CLOSED
    assert _row_counts(db, b)  # untouched


def test_retries_stop_after_the_attempt_limit(
    db: str, users: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = users

    def broken(*_args: Any, **_kwargs: Any) -> int:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ad, "_erase_step", broken)
    request = ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
    moment = AFTER_WINDOW
    for _ in range(ad.MAX_ATTEMPTS + 2):
        ad.process_due_deletions(db, now=moment)
        moment += ad.RETRY_BACKOFF * 2

    status = ad.get_status(db, request["request_id"], now=moment)
    assert status is not None
    assert status["state"] == "failed"
    assert status["attempts"] == ad.MAX_ATTEMPTS
    assert status["error_code"] == "database_error"
    assert (
        ad.account_gate(db, a) is ad.AccountGate.LOCKED
    )  # stays fenced for an operator


def test_a_crashed_run_is_resumed_only_after_its_lease_expires(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    request = ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
    assert (
        ad._claim(db, request["request_id"], AFTER_WINDOW) is not None
    )  # worker dies here

    during = ad.execute_deletion(
        db, request["request_id"], now=AFTER_WINDOW + timedelta(minutes=1)
    )
    assert during is not None and during["state"] == "in_progress"
    assert _row_counts(db, a)

    resumed = ad.execute_deletion(
        db, request["request_id"], now=AFTER_WINDOW + ad.LEASE * 2
    )
    assert resumed is not None and resumed["state"] == "completed"
    assert _row_counts(db, a) == {}


def test_two_workers_cannot_claim_the_same_request(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    request = ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
    assert ad._claim(db, request["request_id"], AFTER_WINDOW) is not None
    assert ad._claim(db, request["request_id"], AFTER_WINDOW) is None


def test_a_row_written_between_sweep_and_close_triggers_another_pass(
    db: str, users: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = users
    real_run_step = ad._run_step
    injected: list[bool] = []

    def run_step(*args: Any) -> None:
        real_run_step(*args)
        if args[4] is ad.Step.UNREGISTERED_STORES and not injected:
            injected.append(True)
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "INSERT INTO chat_messages (role, content, created_at, user_id) "
                    "VALUES ('assistant', 'late', '2026-01-02T00:00:00+00:00', ?)",
                    (a,),
                )

    monkeypatch.setattr(ad, "_run_step", run_step)
    status = _close_account(db, a, email=EMAIL_A)

    assert injected
    assert status["state"] == "completed"
    assert _row_counts(db, a) == {}
    derived = next(s for s in status["steps"] if s["step"] == "derived_insights")
    assert derived["rows_affected"] == 6  # the five seeded rows plus the late one


def test_straggler_sweep_removes_late_writes_for_a_just_closed_account(
    db: str, users: tuple[str, str]
) -> None:
    a, b = users
    _close_account(db, a, email=EMAIL_A)
    deleted_at = datetime.fromisoformat(
        _scalar(db, "SELECT deleted_at FROM account_tombstones WHERE user_id = ?", a)
    )

    def late_chat(user_id: str) -> None:
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO chat_messages (role, content, created_at, user_id) "
                "VALUES ('assistant', 'late', '2026-01-02T00:00:00+00:00', ?)",
                (user_id,),
            )

    late_chat(a)
    before_b = _row_counts(db, b)
    assert ad.sweep_recent_tombstones(db, now=deleted_at + timedelta(minutes=1)) == 1
    assert _row_counts(db, a) == {}
    assert _row_counts(db, b) == before_b

    # The sweep is deliberately bounded to a short window after closure.
    late_chat(a)
    assert ad.sweep_recent_tombstones(db, now=deleted_at + timedelta(hours=2)) == 0
    assert _row_counts(db, a) == {"chat_messages": 1}


def test_unregistered_user_tables_are_swept_on_account_closure(
    db: str, users: tuple[str, str]
) -> None:
    a, b = users
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE extra_notes (user_id TEXT, note TEXT)")
        conn.execute('CREATE TABLE "odd ""name" (user_id TEXT)')
        for owner in (a, b):
            conn.execute("INSERT INTO extra_notes VALUES (?, 'n')", (owner,))
            conn.execute('INSERT INTO "odd ""name" VALUES (?)', (owner,))

    status = _close_account(db, a, email=EMAIL_A)

    assert status["state"] == "completed"
    swept = next(s for s in status["steps"] if s["step"] == "unregistered_stores")
    assert swept["rows_affected"] == 2
    assert _row_counts(db, a) == {}
    assert _scalar(db, "SELECT COUNT(*) FROM extra_notes WHERE user_id = ?", b) == 1


def test_a_store_can_name_a_non_user_id_owner_column(
    db: str, users: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = users
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE invitations (id TEXT PRIMARY KEY, email TEXT NOT NULL, "
            "accepted_user_id TEXT)"
        )
        conn.executemany(
            "INSERT INTO invitations VALUES (?, ?, ?)",
            [("1", EMAIL_A, a), ("2", EMAIL_B, b), ("3", "open@example.com", None)],
        )
    store = ad.DataStore(
        "invitations", ad.Step.PRIMARY_RECORDS, owner="accepted_user_id"
    )
    monkeypatch.setattr(ad, "USER_DATA_STORES", (*ad.USER_DATA_STORES, store))

    status = _close_account(db, a, email=EMAIL_A)

    assert status["state"] == "completed"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT id FROM invitations ORDER BY id").fetchall() == [
            ("2",),
            ("3",),
        ]


_IMMUTABLE_TABLE = """
CREATE TABLE revisions (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL)
"""
# The documented pattern for an append-only table: immutable for the application,
# deletable only for an account that is being (or has been) deleted.
_IMMUTABLE_TRIGGER = """
CREATE TRIGGER trg_revisions_no_delete BEFORE DELETE ON revisions
WHEN NOT EXISTS (
        SELECT 1 FROM account_deletion_requests
        WHERE user_id = OLD.user_id AND state = 'in_progress')
 AND NOT EXISTS (SELECT 1 FROM account_tombstones WHERE user_id = OLD.user_id)
BEGIN SELECT RAISE(ABORT, 'revisions are immutable'); END
"""


@pytest.fixture
def immutable_revisions(
    db: str, users: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(_IMMUTABLE_TABLE)
        conn.execute(_IMMUTABLE_TRIGGER)
        conn.executemany(
            "INSERT INTO revisions (user_id) VALUES (?)",
            [(users[0],), (users[0],), (users[1],)],
        )
    store = ad.DataStore("revisions", ad.Step.PRIMARY_RECORDS)
    monkeypatch.setattr(ad, "USER_DATA_STORES", (*ad.USER_DATA_STORES, store))


def test_append_only_tables_are_erased_only_for_the_account_being_deleted(
    db: str, users: tuple[str, str], immutable_revisions: None
) -> None:
    a, b = users
    # The guard is intact for everyone outside a deletion.
    with (
        sqlite3.connect(db) as conn,
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
    ):
        conn.execute("DELETE FROM revisions WHERE user_id = ?", (a,))

    status = _close_account(db, a, email=EMAIL_A)

    assert status["state"] == "completed"
    assert _scalar(db, "SELECT COUNT(*) FROM revisions WHERE user_id = ?", a) == 0
    # Another tenant's rows stay immutable and untouched.
    assert _scalar(db, "SELECT COUNT(*) FROM revisions WHERE user_id = ?", b) == 1
    with (
        sqlite3.connect(db) as conn,
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
    ):
        conn.execute("DELETE FROM revisions WHERE user_id = ?", (b,))


def test_append_only_tables_are_erased_when_a_restore_is_reconciled(
    db: str, users: tuple[str, str], immutable_revisions: None
) -> None:
    a, b = users
    assert ad.reapply_tombstones(db, [a]) == 1
    assert _scalar(db, "SELECT COUNT(*) FROM revisions WHERE user_id = ?", a) == 0
    assert _scalar(db, "SELECT COUNT(*) FROM revisions WHERE user_id = ?", b) == 1


def test_a_blocked_erasure_fails_the_request_instead_of_hiding_the_rows(
    db: str, users: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = users
    with sqlite3.connect(db) as conn:
        conn.execute(_IMMUTABLE_TABLE)
        conn.execute(
            "CREATE TRIGGER trg_revisions_no_delete BEFORE DELETE ON revisions "
            "BEGIN SELECT RAISE(ABORT, 'revisions are immutable'); END"  # no exemption
        )
        conn.execute("INSERT INTO revisions (user_id) VALUES (?)", (a,))
    store = ad.DataStore("revisions", ad.Step.PRIMARY_RECORDS)
    monkeypatch.setattr(ad, "USER_DATA_STORES", (*ad.USER_DATA_STORES, store))

    request = ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
    status = ad.execute_deletion(db, request["request_id"], now=AFTER_WINDOW)

    assert status is not None
    assert status["state"] == "failed"
    assert status["error_code"] == "database_error"
    assert _scalar(db, "SELECT COUNT(*) FROM users WHERE id = ?", a) == 1
    assert _scalar(db, "SELECT COUNT(*) FROM account_tombstones") == 0
    assert ad.account_gate(db, a) is ad.AccountGate.LOCKED


def test_an_unregistered_search_index_fails_closed(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE VIRTUAL TABLE chat_search USING fts5(content)")
    request = ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
    status = ad.execute_deletion(db, request["request_id"], now=AFTER_WINDOW)
    assert status is not None
    assert status["state"] == "failed"
    assert status["error_code"] == "residual_data"
    assert _scalar(db, "SELECT COUNT(*) FROM users WHERE id = ?", a) == 1


# ---------------------------------------------------------------------------
# Retained exports (filesystem artefacts)
# ---------------------------------------------------------------------------


def test_retained_exports_are_deleted_for_the_user_only(
    db: str, users: tuple[str, str]
) -> None:
    a, b = users
    for owner in (a, b):
        directory = ad.export_directory(db, owner)
        assert directory is not None
        (directory / "nested").mkdir(parents=True)
        (directory / "export.json").write_text("{}")
        (directory / "nested" / "export.csv").write_text("x")

    status = _close_account(db, a, email=EMAIL_A)

    dir_a, dir_b = ad.export_directory(db, a), ad.export_directory(db, b)
    assert dir_a is not None and dir_b is not None
    assert not dir_a.exists()
    assert (dir_b / "export.json").exists()
    exports = next(s for s in status["steps"] if s["step"] == "retained_exports")
    assert exports["rows_affected"] == 2 and exports["remaining_after"] == 0


def test_health_scope_also_removes_retained_exports(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    directory = ad.export_directory(db, a)
    assert directory is not None
    directory.mkdir(parents=True)
    (directory / "export.json").write_text("{}")

    request = ad.request_deletion(db, a, "health_data", ad.HEALTH_CONFIRMATION, now=NOW)
    ad.execute_deletion(db, request["request_id"], now=NOW)

    assert not directory.exists()


def test_a_symlinked_export_directory_is_unlinked_not_followed(
    db: str, users: tuple[str, str], tmp_path: Path
) -> None:
    a, _ = users
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    directory = ad.export_directory(db, a)
    assert directory is not None
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.symlink_to(outside, target_is_directory=True)

    _close_account(db, a, email=EMAIL_A)

    assert not directory.exists() and not directory.is_symlink()
    assert (outside / "keep.txt").read_text() == "keep"


@pytest.mark.parametrize(
    "hostile", ["../escape", "a/b", "..", "", "x" * 65, "a\x00b", "/etc"]
)
def test_export_directory_never_leaves_the_exports_root(db: str, hostile: str) -> None:
    assert ad.export_directory(db, hostile) is None


# ---------------------------------------------------------------------------
# Progress and status documents
# ---------------------------------------------------------------------------


def test_progress_reflects_completed_steps(
    db: str, users: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = users
    real_erase = ad._erase_step

    def fail_at_caches(
        conn: sqlite3.Connection, db_path: str, uid: str, scope: Any, step: Any
    ) -> int:
        if step is ad.Step.CACHES:
            raise OSError("synthetic")
        return real_erase(conn, db_path, uid, scope, step)

    monkeypatch.setattr(ad, "_erase_step", fail_at_caches)
    request = ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
    status = ad.execute_deletion(db, request["request_id"], now=AFTER_WINDOW)

    assert status is not None
    assert status["error_code"] == "filesystem_error"
    done = [s["step"] for s in status["steps"] if s["state"] == "done"]
    assert done == [
        "queued_jobs",
        "notifications",
        "connector_tokens",
        "derived_insights",
        "primary_records",
    ]
    assert status["progress_percent"] == 50
    assert all(s["label"] for s in status["steps"])


def test_describe_lists_scopes_with_exact_confirmation_text(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    info = ad.describe(db, a, now=NOW)

    assert info["eligible"] is True
    scopes = {item["scope"]: item for item in info["scopes"]}
    assert scopes["account"]["required_confirmation"] == EMAIL_A
    assert scopes["account"]["grace_days"] == 7
    assert scopes["health_data"]["required_confirmation"] == ad.HEALTH_CONFIRMATION
    assert scopes["health_data"]["grace_days"] == 0
    assert info["request"] is None

    ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
    pending = ad.describe(db, a, now=NOW)["request"]
    assert pending is not None
    assert pending["state"] == "pending" and pending["can_cancel"] is True


def test_describe_prefers_the_open_request_over_history(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    done = ad.request_deletion(db, a, "health_data", ad.HEALTH_CONFIRMATION, now=NOW)
    ad.execute_deletion(db, done["request_id"], now=NOW)
    open_request = ad.request_deletion(
        db, a, "account", EMAIL_A, now=NOW + timedelta(minutes=1)
    )

    latest = ad.describe(db, a, now=NOW)["request"]
    assert latest is not None and latest["request_id"] == open_request["request_id"]


def test_status_lookup_of_an_unknown_request_is_none(db: str) -> None:
    assert ad.get_status(db, "missing") is None
    assert ad.execute_deletion(db, "missing", now=NOW) is None


# ---------------------------------------------------------------------------
# Consumers: CSRF, background jobs and the scheduler loop
# ---------------------------------------------------------------------------


def test_csrf_store_reports_closed_accounts(db: str, users: tuple[str, str]) -> None:
    a, b = users
    store = CSRFTokenStore(db)
    assert store.is_closed(a) is False
    _close_account(db, a, email=EMAIL_A)
    assert store.is_closed(a) is True
    assert store.is_closed(b) is False
    assert store.is_closed("never-existed") is False


def test_csrf_store_without_ledger_table_is_not_closed(tmp_path: Path) -> None:
    assert CSRFTokenStore(str(tmp_path / "csrf-only.db")).is_closed("anyone") is False


def test_insight_jobs_skip_accounts_with_a_deletion_request(
    db: str, users: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import insight_cron
    from config import Config

    a, b = users

    def must_not_resolve(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an AI provider was resolved for a closing account")

    monkeypatch.setattr(insight_cron, "_resolve_provider", must_not_resolve)
    config = Config(
        hevy_api_key=None,
        gemini_api_key=None,
        gemini_model="synthetic",
        health_connect_file=None,
        database_path=db,
        hevy_sync_routines=False,
        hevy_prefill_weights=False,
        checkin_enabled=False,
        lifestyle_enabled=False,
        google_health_client_id=None,
        google_health_client_secret=None,
        google_health_refresh_token=None,
        self_review_enabled=False,
        self_review_weekday=6,
    )
    ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)

    insight_cron.generate_daily_header(config, user_id=a)
    insight_cron.generate_weekly_correlations(config, user_id=a)

    seen: list[str] = []
    monkeypatch.setattr(
        insight_cron,
        "_resolve_provider",
        lambda _config, owner: (
            seen.append(owner.user_id) or (_ for _ in ()).throw(RuntimeError("stop"))
        ),
    )
    with pytest.raises(RuntimeError, match="stop"):
        insight_cron.generate_daily_header(config, user_id=b)
    assert seen == [b]


def test_daily_run_does_nothing_for_an_account_with_a_deletion_request(
    db: str, users: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import main
    from config import Config

    a, _ = users
    config = Config(
        hevy_api_key="synthetic",
        gemini_api_key=None,
        gemini_model="synthetic",
        health_connect_file=None,
        database_path=db,
        hevy_sync_routines=False,
        hevy_prefill_weights=False,
        checkin_enabled=False,
        lifestyle_enabled=False,
        google_health_client_id=None,
        google_health_client_secret=None,
        google_health_refresh_token=None,
        self_review_enabled=False,
        self_review_weekday=6,
    )
    monkeypatch.setattr(main.Config, "load", lambda: config)

    def must_not_sync(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("work started for a closing account")

    monkeypatch.setattr(main, "sync_routines", must_not_sync)
    monkeypatch.setattr(main, "_scope_config_to_user", must_not_sync)
    ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)

    assert main.run(preview=True, user_id=a) == 0


def test_scheduler_runs_due_deletions_first_and_survives_their_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    import scheduler

    calls: list[str] = []
    config = MagicMock(database_path="synthetic.db")
    monkeypatch.setattr(
        scheduler,
        "process_due_deletions",
        lambda path: calls.append(f"deletions:{path}") or 2,
    )
    monkeypatch.setattr(
        scheduler, "run_coaching", lambda cfg: calls.append("coaching") or 0
    )
    monkeypatch.setattr(
        scheduler, "run_daily_insight", lambda cfg: calls.append("daily")
    )
    monkeypatch.setattr(
        scheduler, "_now_in_zone", lambda tz: datetime(2026, 11, 9, tzinfo=timezone.utc)
    )

    assert scheduler.run_account_deletions(config) == 2
    calls.clear()
    scheduler.run_once(config)
    assert calls[:2] == ["deletions:synthetic.db", "coaching"]

    def boom(path: str) -> int:
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(scheduler, "process_due_deletions", boom)
    assert scheduler.run_account_deletions(config) == 0


def test_worker_tick_completes_due_requests_and_reports_the_count(
    db: str, users: tuple[str, str]
) -> None:
    a, b = users
    for owner, email in ((a, EMAIL_A), (b, EMAIL_B)):
        ad.request_deletion(db, owner, "account", email, now=NOW)

    assert ad.process_due_deletions(db, now=NOW + timedelta(days=1)) == 0
    assert ad.process_due_deletions(db, now=AFTER_WINDOW) == 2
    assert ad.process_due_deletions(db, now=AFTER_WINDOW) == 0
    assert _row_counts(db, a) == {} and _row_counts(db, b) == {}


# ---------------------------------------------------------------------------
# Operator tools: a restored backup must not resurrect erased accounts
# ---------------------------------------------------------------------------


def _snapshot(db: str, destination: Path) -> str:
    """A consistent copy of *db*, as a backup would capture it."""
    target = str(destination)
    with sqlite3.connect(db) as source, sqlite3.connect(target) as copy:
        source.backup(copy)
    return target


def test_reapplying_tombstones_erases_accounts_a_restore_brought_back(
    db: str, users: tuple[str, str], tmp_path: Path
) -> None:
    a, b = users
    backup = _snapshot(
        db, tmp_path / "backup.db"
    )  # taken before A closed their account
    _close_account(db, a, email=EMAIL_A)
    closed_ids = ad.list_tombstoned_ids(db)
    assert closed_ids == [a]

    # Restoring the old backup brings A's account and data back, without the tombstone.
    assert _row_counts(backup, a) != {}
    assert ad.account_gate(backup, a) is ad.AccountGate.OPEN
    before_b = _row_counts(backup, b)

    assert ad.reapply_tombstones(backup, closed_ids) == 1

    assert _row_counts(backup, a) == {}
    assert _row_counts(backup, b) == before_b
    assert _scalar(backup, "SELECT COUNT(*) FROM users WHERE id = ?", a) == 0
    assert ad.account_gate(backup, a) is ad.AccountGate.CLOSED
    assert ad.reapply_tombstones(backup, closed_ids) == 0  # idempotent


def test_reapplying_a_tombstone_for_an_account_absent_from_the_backup_still_fences_it(
    db: str,
) -> None:
    assert ad.reapply_tombstones(db, ["created-after-the-backup", "  ", ""]) == 1
    assert ad.account_gate(db, "created-after-the-backup") is ad.AccountGate.CLOSED


def test_reapplying_tombstones_never_closes_the_legacy_tenant(db: str) -> None:
    legacy = _scalar(db, "SELECT id FROM users WHERE email = 'legacy@local'")
    with pytest.raises(ad.DeletionError) as excinfo:
        ad.reapply_tombstones(db, [legacy])
    assert excinfo.value.code == "not_eligible"
    assert _scalar(db, "SELECT COUNT(*) FROM users WHERE id = ?", legacy) == 1
    assert _scalar(db, "SELECT COUNT(*) FROM account_tombstones") == 0


def test_cli_lists_and_reapplies_tombstones(
    db: str,
    users: tuple[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    a, _ = users
    backup = _snapshot(db, tmp_path / "restored.db")
    _close_account(db, a, email=EMAIL_A)

    assert ad.main(["--database", db, "list-tombstones"]) == 0
    listed = capsys.readouterr().out
    assert listed.split() == [a]

    ids_file = tmp_path / "tombstoned.txt"
    ids_file.write_text(listed)
    assert ad.main(["--database", backup, "reconcile", str(ids_file)]) == 0
    assert "Re-applied 1 deletion(s)." in capsys.readouterr().out
    assert _row_counts(backup, a) == {}
    assert ad.account_gate(backup, a) is ad.AccountGate.CLOSED
