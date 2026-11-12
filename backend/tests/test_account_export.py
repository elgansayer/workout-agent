"""Deterministic tests for the complete, asynchronous account data export."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import stat
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import account_deletion as ad
import account_export as ae
import pytest
import scheduler
import security_audit as audit
from data_classification import DataClass, classify_field
from database import (
    correct_body_metrics,
    get_or_create_user,
    init_db,
    save_body_metrics,
    save_chat_message,
    save_checkin,
    save_daily_log,
    save_dashboard_insight,
    save_deep_correlation,
    save_notification,
    save_push_subscription,
    save_reasoning_log,
    save_user_api_key,
    save_user_preferences,
    save_workout,
)
from provenance import Provenance
from webapp.csrf_security import CSRFTokenStore

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
EMAIL_A = "athlete.a@example.com"
EMAIL_B = "athlete.b@example.com"
ROOT = ae.ARCHIVE_ROOT
_KEY = audit.resolve_audit_key({"AUDIT_LOG_HMAC_KEY": "k" * 40})
_AI = "ai:gemini/gemini-2.5-flash"


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())
    for name in (
        "ACCOUNT_EXPORT_RETENTION_HOURS",
        "ACCOUNT_EXPORT_MAX_MB",
        "ACCOUNT_EXPORT_DAILY_LIMIT",
        "ACCOUNT_DELETION_GRACE_DAYS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "data" / "workout.db")
    init_db(path)
    CSRFTokenStore(path)._connect().close()  # lazily-created cache table
    return path


@pytest.fixture
def users(db: str) -> tuple[str, str]:
    a = get_or_create_user(EMAIL_A, "Athlete A", db_path=db)["id"]
    b = get_or_create_user(EMAIL_B, "Athlete B", db_path=db)["id"]
    return a, b


def _secrets(tag: str) -> dict[str, str]:
    """Values that must never reach any archive, whoever asks."""
    return {
        name: f"SECRET-{tag}-{name}"
        for name in (
            "HEVY-KEY",
            "CLIENT-ID",
            "CLIENT-SECRET",
            "REFRESH",
            "EXTRA",
            "ENDPOINT",
            "P256",
            "AUTH",
            "SESSION",
            "CSRF",
            "SUBJECT",
            "GH-REFRESH",
            "GH-STATE",
            "CURSOR",
            "PAYLOAD-TOKEN",
            "OPERATOR",
        )
    }


def _seed(db: str, user_id: str, tag: str) -> dict[str, str]:
    """Give *user_id* synthetic rows in every exported store; return its secrets."""
    secrets = _secrets(tag)
    stamp = "2026-01-01T00:00:00+00:00"
    derived = Provenance.derived(_AI, "synthetic/1", confidence=0.7)
    save_user_preferences(
        user_id,
        goals=["strength"],
        constraints=[f"constraint-{tag}"],
        experience_level="intermediate",
        custom_rules=[f"rule-{tag}"],
        db_path=db,
    )
    save_workout(
        {
            "id": f"workout-{tag}",
            "title": f"Workout {tag}",
            "start_time": "2026-01-01T10:00:00Z",
            "token": secrets["PAYLOAD-TOKEN"],
        },
        db,
        "2026-01-01",
        user_id=user_id,
    )
    save_body_metrics(
        {"weight_kg": 80.5, "body_fat_pct": 18.0},
        "2026-01-02",
        db,
        user_id=user_id,
        provenance=Provenance.imported("google_health", "google-health-body/1"),
    )
    correct_body_metrics(
        user_id, "2026-01-02", {"weight_kg": 81.0}, reason=f"reason-{tag}", db_path=db
    )
    save_chat_message("user", f"question-{tag}", db_path=db, user_id=user_id)
    save_chat_message("assistant", f"answer-{tag}", db_path=db, user_id=user_id)
    save_checkin(1, "2026-01-03", 3, 4, f"check-in-{tag}", db, user_id=user_id, provenance=derived)
    save_daily_log(
        "2026-01-04", 1, "Legs", "high", f"plan-{tag}", "life", db, user_id=user_id, provenance=derived
    )
    save_dashboard_insight(
        json.dumps({"headline": f"insight-{tag}"}), db, user_id=user_id, provenance=derived
    )
    save_deep_correlation(f"correlation-{tag}", db, user_id=user_id, provenance=derived)
    save_reasoning_log("ctx-1", "Squat", f"because-{tag}", db, user_id=user_id, provenance=derived)
    save_notification(user_id, f"Title {tag}", f"Message {tag}", db_path=db)
    save_push_subscription(
        user_id,
        f"https://push.example.invalid/{secrets['ENDPOINT']}",
        secrets["P256"],
        secrets["AUTH"],
        db,
    )
    save_user_api_key(
        user_id,
        "hevy",
        secrets["HEVY-KEY"],
        client_id=secrets["CLIENT-ID"],
        client_secret=secrets["CLIENT-SECRET"],
        refresh_token=secrets["REFRESH"],
        extra={"note": secrets["EXTRA"]},
        db_path=db,
    )
    with sqlite3.connect(db) as conn:
        run = conn.execute
        run(
            "INSERT INTO training_rules (user_id, version, rules_json, updated_at) "
            "VALUES (?, 1, ?, ?)",
            (user_id, json.dumps({"max_sessions_per_week": 4}), stamp),
        )
        run(
            "INSERT INTO onboarding_skips (user_id, step, skipped_at) VALUES (?, 'notifications', ?)",
            (user_id, stamp),
        )
        run(
            "INSERT INTO user_identities (user_id, provider, subject, email, email_verified, created_at) "
            "VALUES (?, 'google', ?, ?, 1, ?)",
            (user_id, secrets["SUBJECT"], f"{tag.lower()}@example.com", stamp),
        )
        run(
            "INSERT INTO account_invitations "
            "(id, email, status, invited_by, created_at, expires_at, accepted_at, accepted_user_id) "
            "VALUES (?, ?, 'accepted', ?, ?, ?, ?, ?)",
            (f"inv-{tag}", f"{tag.lower()}@example.com", secrets["OPERATOR"], stamp, stamp, stamp, user_id),
        )
        run(
            "INSERT INTO user_sessions (session_token_hash, user_id, created_at, last_seen_at) "
            "VALUES (?, ?, 1, 1)",
            (secrets["SESSION"], user_id),
        )
        run(
            "INSERT INTO csrf_tokens (token_hash, session_hash, user_id, expires_at) "
            "VALUES (?, 's', ?, 9999999999)",
            (secrets["CSRF"], user_id),
        )
        for key, value in (
            ("checkin_number", "1"),
            ("routine_folder_id", f"folder-{tag}"),
            ("connector:hevy:last_sync", json.dumps({"outcome": "succeeded", "records": 3})),
            ("connector:hevy:operation", "lease-is-operational"),
            ("google_health_refresh_token", secrets["GH-REFRESH"]),
            ("google_health_oauth_state", secrets["GH-STATE"]),
        ):
            run(
                "INSERT INTO hevy_meta (user_id, key, value) VALUES (?, ?, ?)",
                (user_id, key, value),
            )
        run("INSERT INTO programme_state (user_id, current_day, split_name) VALUES (?, 2, 'Split')", (user_id,))
        run(
            "INSERT INTO exercise_progress (date, exercise_name, top_weight_kg, top_reps, sets, user_id) "
            "VALUES ('2026-01-01', 'Squat', 100, 5, 3, ?)",
            (user_id,),
        )
        run(
            "INSERT INTO hevy_routines (user_id, routine_key, routine_id, content_hash) VALUES (?, 'k', 'r', 'h')",
            (user_id,),
        )
        programme_id = run(
            "INSERT INTO programmes (user_id, source, template_key, definition, active, created_at, updated_at, name) "
            "VALUES (?, 'hevy', 'tpl', ?, 1, ?, ?, ?)",
            (
                user_id,
                json.dumps(
                    {"engine_version": "programme-engine/3", "analysis": {"confidence": "high"}}
                ),
                stamp,
                stamp,
                f"Programme {tag}",
            ),
        ).lastrowid
        revision_id = run(
            "INSERT INTO programme_revisions "
            "(user_id, programme_id, revision_number, reason, engine_version, settings_json, created_at) "
            "VALUES (?, ?, 1, 'synthetic', 'programme-engine/3', '{}', ?)",
            (user_id, programme_id, stamp),
        ).lastrowid
        run(
            "INSERT INTO programme_routines "
            "(user_id, programme_id, revision_id, position, source_routine_id, title, snapshot_hash, "
            "snapshot_json, enabled, created_at) VALUES (?, ?, ?, 1, 'routine', 'Routine', 'hash', '{}', 1, ?)",
            (user_id, programme_id, revision_id, stamp),
        )
        run(
            "INSERT INTO programme_blocks "
            "(user_id, programme_id, revision_id, position, key, name, start_week, end_week, created_at) "
            "VALUES (?, ?, ?, 1, 'block', 'Block', 1, 4, ?)",
            (user_id, programme_id, revision_id, stamp),
        )
        run(
            "INSERT INTO exercise_prescriptions "
            "(user_id, programme_id, revision_id, exercise_template_id, exercise_title, role, "
            "progression_method, confidence, rule_version, created_at) "
            "VALUES (?, ?, ?, 'tpl', 'Squat', 'primary', 'linear', 0.6, 'prescription/2', ?)",
            (user_id, programme_id, revision_id, stamp),
        )
        run(
            "INSERT INTO programme_week_states (user_id, programme_id, revision_id, week_number, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (user_id, programme_id, revision_id, stamp, stamp),
        )
        run(
            "INSERT INTO adaptation_decisions "
            "(user_id, programme_id, to_revision_id, confidence, rule_version, explanation, confirmed, created_at) "
            "VALUES (?, ?, ?, 0.8, 'adapt/4', ?, 1, ?)",
            (user_id, programme_id, revision_id, f"explanation-{tag}", stamp),
        )
        run(
            "INSERT INTO programme_decisions (user_id, programme_id, source, decision, reason, created_at) "
            "VALUES (?, ?, 'hevy', 'keep', 'synthetic', ?)",
            (user_id, programme_id, stamp),
        )
        run(
            "INSERT INTO hevy_routine_snapshots "
            "(user_id, snapshot_id, routine_id, title, content_hash, schema_version, snapshot_json) "
            "VALUES (?, 'snap', 'routine', 'Routine', 'hash', 1, '{}')",
            (user_id,),
        )
        run(
            "INSERT INTO routine_workout_matches "
            "(user_id, programme_id, routine_position, source_routine_id, workout_id, score, confidence, "
            "features_json, method, matcher_version, is_ambiguous, matched_at, created_at) "
            "VALUES (?, ?, 1, 'routine', 'workout', 0.9, 0.9, '{}', 'title', 'matcher/5', 0, ?, ?)",
            (user_id, programme_id, stamp, stamp),
        )
        run(
            "INSERT INTO routine_match_overrides "
            "(user_id, pattern_type, pattern_value, routine_id, routine_snapshot_id, created_at) "
            "VALUES (?, 'title', 'push', 'routine', 'snap', ?)",
            (user_id, stamp),
        )
        run(
            "INSERT INTO exercise_history_baselines "
            "(user_id, template_id, rule_version, confidence, baseline_json, created_at) "
            "VALUES (?, 'tpl', 'baseline/1', 'high', '{}', ?)",
            (user_id, stamp),
        )
        run(
            "INSERT INTO hevy_connections "
            "(user_id, provider, state, account_username, capabilities_json, tested_at, created_at, updated_at) "
            "VALUES (?, 'hevy', 'connected', ?, '{}', ?, ?, ?)",
            (user_id, f"lifter-{tag}", stamp, stamp, stamp),
        )
        run(
            "INSERT INTO health_connections (id, user_id, provider, state, cursor) "
            "VALUES (?, ?, 'oura', 'connected', ?)",
            (f"conn-{tag}", user_id, secrets["CURSOR"]),
        )
        run(
            "INSERT INTO health_metrics (user_id, metric_type, value, unit, observed_at, provider, "
            "connection_id, normalization_version, fingerprint) "
            "VALUES (?, 'hrv', 60, 'ms', ?, 'oura', ?, 2, ?)",
            (user_id, stamp, f"conn-{tag}", f"fingerprint-{tag}"),
        )
        run(
            "INSERT INTO health_sync_runs (user_id, provider, connection_id, state, started_at, cursor_after) "
            "VALUES (?, 'oura', ?, 'ok', ?, ?)",
            (user_id, f"conn-{tag}", stamp, secrets["CURSOR"]),
        )
        run(
            "INSERT INTO credential_audit_events "
            "(user_id, provider, action, outcome, detail, actor, source, credential_version, created_at) "
            "VALUES (?, 'hevy', 'store', 'success', 'verified', 'owner', 'web', 1, ?)",
            (user_id, stamp),
        )
        run(
            "INSERT INTO account_deletion_requests "
            "(id, user_id, scope, state, requested_at, execute_after, next_attempt_at, cancelled_at) "
            "VALUES (?, ?, 'account', 'cancelled', ?, ?, ?, ?)",
            (f"receipt-{tag}", user_id, stamp, stamp, stamp, stamp),
        )
    audit.record_security_event(
        db,
        audit.SecurityEvent(
            event_type=audit.AuditEventType.CREDENTIAL_SAVED,
            outcome=audit.AuditOutcome.SUCCESS,
            actor_type=audit.ActorType.USER,
            actor_id=user_id,
            user_id=user_id,
            subject_type=audit.AuditSubjectType.CREDENTIAL,
            subject_id="hevy",
            request_id=f"request-{tag}",
            source_ref=f"client-pseudonym-{tag}",
            metadata={"provider": "hevy"},
        ),
        key=_KEY,
    )
    audit.record_security_event(
        db,
        audit.SecurityEvent(
            event_type=audit.AuditEventType.ALERT_CONNECTOR_ABUSE,
            outcome=audit.AuditOutcome.RAISED,
            actor_type=audit.ActorType.SYSTEM,
            user_id=user_id,
            subject_type=audit.AuditSubjectType.SYSTEM,
            metadata={"alert": f"alert-{tag}"},
        ),
        key=_KEY,
    )
    return secrets


def _export(db: str, user_id: str, *, now: datetime = NOW) -> ae.ExportStatus:
    """Request an export and run the worker, as the web app and scheduler would."""
    status, created = ae.request_export(db, user_id, now=now)
    assert created
    ae.process_due_exports(db, now=now)
    done = ae.get_export(db, user_id, status["export_id"], now=now)
    assert done is not None
    return done


def _open(db: str, user_id: str, export_id: str) -> zipfile.ZipFile:
    return zipfile.ZipFile(ae.open_download(db, user_id, export_id, now=NOW).path)


def _text(archive: zipfile.ZipFile) -> str:
    return "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist())


def _records(archive: zipfile.ZipFile, path: str) -> list[dict[str, Any]]:
    document = json.loads(archive.read(f"{ROOT}/{path}"))
    assert document["record_count"] == len(document["records"])
    return list(document["records"])


def _rows(db: str, sql: str, *params: Any) -> list[sqlite3.Row]:
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute(sql, params))


# ---------------------------------------------------------------------------
# The manifest: nothing is exported - or withheld - by accident
# ---------------------------------------------------------------------------


def test_every_column_is_exported_or_withheld(db: str) -> None:
    """A new column on an exported table stays out until a person reviews it."""
    with sqlite3.connect(db) as conn:
        for spec in ae.EXPORT_TABLES:
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{spec.table}")')}
            assert columns, f"{spec.table} does not exist"
            assert set(spec.columns) | set(spec.withheld) == columns, spec.table
            assert not set(spec.columns) & set(spec.withheld), spec.table
            assert all(spec.withheld.values()), f"{spec.table} withholds without a reason"


def test_every_account_owning_table_is_reviewed(db: str) -> None:
    with sqlite3.connect(db) as conn:
        assert ae.unreviewed_owner_tables(conn) == []
        owned = {
            row[0]
            for row in conn.execute(
                "SELECT m.name FROM sqlite_master m JOIN pragma_table_info(m.name) c "
                "WHERE m.type = 'table' AND (c.name = 'user_id' OR c.name LIKE '%\\_user\\_id' ESCAPE '\\')"
            )
        }
    reviewed = {spec.table for spec in ae.EXPORT_TABLES} | set(ae.EXCLUDED_TABLES)
    assert owned <= reviewed
    assert all(ae.EXCLUDED_TABLES.values())


def test_an_unreviewed_table_is_reported_not_silently_dropped(
    db: str, users: tuple[str, str]
) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE future_feature (id INTEGER PRIMARY KEY, user_id TEXT, note TEXT)")
        conn.execute("INSERT INTO future_feature (user_id, note) VALUES (?, 'FUTURE-ROW')", (users[0],))
    archive = _open(db, users[0], _export(db, users[0])["export_id"])
    manifest = json.loads(archive.read(f"{ROOT}/manifest.json"))
    assert manifest["unreviewed_tables"] == ["future_feature"]
    assert "FUTURE-ROW" not in _text(archive)


def test_credential_class_columns_can_never_be_listed() -> None:
    with pytest.raises(ValueError, match="credential-class"):
        ae.ExportTable(
            path="connectors/credentials.json",
            table="user_api_keys",
            description="x",
            columns=("provider", "api_key"),
            order_by="t.provider",
        )
    for spec in ae.EXPORT_TABLES:
        assert all(classify_field(column) is not DataClass.CREDENTIAL for column in spec.columns)


def test_credential_columns_of_exported_tables_are_withheld(db: str) -> None:
    with sqlite3.connect(db) as conn:
        for spec in ae.EXPORT_TABLES:
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{spec.table}")')}
            credential = {c for c in columns if classify_field(c) is DataClass.CREDENTIAL}
            assert credential <= set(spec.withheld), (spec.table, credential - set(spec.withheld))


def test_required_categories_are_all_present() -> None:
    required = {
        "profile",
        "programmes",
        "workouts",
        "metrics",
        "check_ins",
        "connectors",
        "notifications",
        "coaching",
        "audit",
    }
    assert required <= set(ae.CATEGORIES)
    assert {spec.category for spec in ae.EXPORT_TABLES} == set(ae.CATEGORIES)


def test_documentation_names_every_file_and_exclusion() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "ACCOUNT_EXPORT.md").read_text("utf-8")
    for spec in ae.EXPORT_TABLES:
        assert spec.path in doc, spec.path
    for table in ae.EXCLUDED_TABLES:
        assert table in doc, table
    for name, _reason in ae.EXCLUDED_DATA:
        assert name in doc, name


# ---------------------------------------------------------------------------
# The archive: complete, honest and tenant-scoped
# ---------------------------------------------------------------------------


def test_archive_holds_a_populated_file_for_every_category(
    db: str, users: tuple[str, str]
) -> None:
    a, b = users
    _seed(db, a, "A")
    _seed(db, b, "B")
    archive = _open(db, a, _export(db, a)["export_id"])
    manifest = json.loads(archive.read(f"{ROOT}/manifest.json"))

    assert manifest["format"] == ae.EXPORT_FORMAT
    assert manifest["format_version"] == ae.EXPORT_FORMAT_VERSION
    paths = {item["path"].removeprefix(f"{ROOT}/") for item in manifest["files"]}
    assert paths == {spec.path for spec in ae.EXPORT_TABLES}
    empty = [item["path"] for item in manifest["files"] if item["records"] == 0]
    assert empty == [], f"the seed must exercise every file: {empty}"
    assert {item["category"] for item in manifest["files"]} == set(ae.CATEGORIES)
    assert manifest["totals"]["files"] == len(manifest["files"]) == len(ae.EXPORT_TABLES)
    assert manifest["unreviewed_tables"] == []
    assert manifest["account_id"] == a
    assert {entry["category"] for entry in manifest["excluded"]} == {n for n, _ in ae.EXCLUDED_DATA}
    assert manifest["preferences"]["stored_weight_unit"] == "kg"
    assert f"{ROOT}/{ae.README_NAME}" in archive.namelist()


def test_manifest_integrity_matches_the_files(db: str, users: tuple[str, str]) -> None:
    a, _ = users
    _seed(db, a, "A")
    archive = _open(db, a, _export(db, a)["export_id"])
    manifest = json.loads(archive.read(f"{ROOT}/manifest.json"))
    assert archive.testzip() is None
    for item in manifest["files"]:
        data = archive.read(item["path"])
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
        assert len(data) == item["bytes"]
        document = json.loads(data)
        assert document["record_count"] == item["records"]
        assert document["schema_version"] == item["schema_version"] >= 1
    assert manifest["totals"]["records"] == sum(item["records"] for item in manifest["files"])


def test_profile_programmes_workouts_metrics_and_coaching_content(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    _seed(db, a, "A")
    archive = _open(db, a, _export(db, a)["export_id"])

    account = _records(archive, "profile/account.json")
    assert [(r["id"], r["email"]) for r in account] == [(a, EMAIL_A)]
    coaching_profile = _records(archive, "profile/coaching_profile.json")[0]
    assert coaching_profile["goals"] == ["strength"]
    assert coaching_profile["constraints"] == ["constraint-A"]
    assert _records(archive, "profile/training_rules.json")[0]["rules_json"] == {
        "max_sessions_per_week": 4
    }
    programme = _records(archive, "programmes/programmes.json")[0]
    assert programme["name"] == "Programme A"
    assert programme["definition"]["engine_version"] == "programme-engine/3"
    assert programme["active"] is True
    workout = _records(archive, "workouts/workouts.json")[0]
    assert workout["hevy_payload"]["title"] == "Workout A"
    assert [r["weight_kg"] for r in _records(archive, "metrics/body_metrics.json")] == [81.0]
    assert _records(archive, "check_ins/check_ins.json")[0]["message"] == "check-in-A"
    assert {r["role"]: r["content"] for r in _records(archive, "coaching/chat_messages.json")} == {
        "user": "question-A",
        "assistant": "answer-A",
    }
    assert _records(archive, "coaching/daily_plans.json")[0]["plan"] == "plan-A"
    assert _records(archive, "coaching/insights.json")[0]["insight_json"] == {"headline": "insight-A"}
    assert _records(archive, "coaching/correlations.json")[0]["insight_markdown"] == "correlation-A"
    assert _records(archive, "coaching/reasoning.json")[0]["reasoning"] == "because-A"
    notification = _records(archive, "notifications/notifications.json")[0]
    assert notification["title"] == "Title A"
    assert notification["is_read"] is False
    assert _records(archive, "connectors/state.json") == [
        {"key": "checkin_number", "value": "1"},
        {"key": "connector:hevy:last_sync", "value": {"outcome": "succeeded", "records": 3}},
        {"key": "routine_folder_id", "value": "folder-A"},
    ]
    assert _records(archive, "connectors/credentials.json")[0]["provider"] == "hevy"
    assert _records(archive, "audit/deletion_requests.json")[0]["state"] == "cancelled"


def test_secrets_internal_security_data_and_other_accounts_are_excluded(
    db: str, users: tuple[str, str]
) -> None:
    a, b = users
    secrets_a = _seed(db, a, "A")
    secrets_b = _seed(db, b, "B")
    text = _text(_open(db, a, _export(db, a)["export_id"]))

    for value in secrets_a.values():
        assert value not in text, value
    internals = _rows(
        db,
        "SELECT event_hash, prev_hash, key_id, source_ref, event_id FROM security_audit_events "
        "WHERE user_id = ?",
        a,
    )
    assert internals
    for row in internals:
        for value in row:
            assert value is None or value not in text, value
    # Nothing that belongs to the other account, secret or not.
    for value in secrets_b.values():
        assert value not in text
    for marker in (
        "question-B",
        "answer-B",
        "plan-B",
        "Programme B",
        "Workout B",
        "insight-B",
        "folder-B",
        "lifter-B",
        "receipt-B",
        "request-B",
        EMAIL_B,
        b,
    ):
        assert marker not in text, marker
    assert "lease-is-operational" not in text  # the operation lease is not exported


def test_audit_events_are_the_owners_user_facing_events_only(
    db: str, users: tuple[str, str]
) -> None:
    a, b = users
    _seed(db, a, "A")
    _seed(db, b, "B")
    audit.record_security_event(
        db,
        audit.SecurityEvent(
            event_type=audit.AuditEventType.LOGIN,
            outcome=audit.AuditOutcome.FAILURE,
            actor_type=audit.ActorType.ANONYMOUS,
            subject_type=audit.AuditSubjectType.IDENTITY,
        ),
        key=_KEY,
    )
    events = _records(_open(db, a, _export(db, a)["export_id"]), "audit/security_events.json")

    assert [(e["event_type"], e["outcome"], e["actor_type"]) for e in events] == [
        ("credential.saved", "success", "user")
    ]
    assert set(events[0]) == {
        "occurred_at",
        "event_type",
        "outcome",
        "severity",
        "actor_type",
        "subject_type",
        "request_id",
    }
    assert events[0]["request_id"] == "request-A"


def test_json_blobs_are_scrubbed_of_credential_keys(db: str, users: tuple[str, str]) -> None:
    a, _ = users
    secrets = _seed(db, a, "A")
    archive = _open(db, a, _export(db, a)["export_id"])
    payload = _records(archive, "workouts/workouts.json")[0]["hevy_payload"]
    assert payload["title"] == "Workout A"
    assert "token" not in payload
    assert secrets["PAYLOAD-TOKEN"] not in _text(archive)
    manifest = json.loads(archive.read(f"{ROOT}/manifest.json"))
    redacted = {item["path"]: item["redacted_fields"] for item in manifest["files"]}
    assert redacted[f"{ROOT}/workouts/workouts.json"] >= 1


def test_derived_data_carries_provenance_and_schema_versions(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    _seed(db, a, "A")
    archive = _open(db, a, _export(db, a)["export_id"])
    manifest = json.loads(archive.read(f"{ROOT}/manifest.json"))

    derived_files = [item for item in manifest["files"] if item["derived"]]
    assert {item["path"].removeprefix(f"{ROOT}/") for item in derived_files} >= {
        "programmes/programmes.json",
        "programmes/revisions.json",
        "programmes/prescriptions.json",
        "programmes/adaptation_decisions.json",
        "workouts/routine_matches.json",
        "workouts/exercise_baselines.json",
        "check_ins/check_ins.json",
        "coaching/chat_messages.json",
        "coaching/daily_plans.json",
        "coaching/insights.json",
        "coaching/correlations.json",
        "coaching/reasoning.json",
    }
    for item in derived_files:
        document = json.loads(archive.read(item["path"]))
        assert document["derived"] is True
        assert document["schema_version"] >= 1
        for record in document["records"]:
            assert "provenance" in record, item["path"]
            view = record["provenance"]
            assert view is not None, item["path"]
            assert view["kind"] in {"derived", "user_entered"}
            assert view["contract_version"] == 1

    plan = _records(archive, "coaching/daily_plans.json")[0]["provenance"]
    assert (plan["kind"], plan["source"], plan["transformation_version"]) == (
        "derived",
        _AI,
        "synthetic/1",
    )
    assert plan["authoritative"] is False
    assert plan["confidence"] == pytest.approx(0.7)
    assert _records(archive, "check_ins/check_ins.json")[0]["provenance"]["source"] == _AI
    assert _records(archive, "coaching/insights.json")[0]["provenance"]["source"] == _AI
    assert _records(archive, "coaching/reasoning.json")[0]["provenance"]["source"] == _AI
    programme = _records(archive, "programmes/programmes.json")[0]["provenance"]
    assert programme["transformation_version"] == "programme-engine/3"
    assert programme["confidence_label"] == "high"
    prescription = _records(archive, "programmes/prescriptions.json")[0]["provenance"]
    assert (prescription["transformation_version"], prescription["confidence"]) == (
        "prescription/2",
        0.6,
    )
    match = _records(archive, "workouts/routine_matches.json")[0]["provenance"]
    assert (match["transformation_version"], match["method"]) == ("matcher/5", "title")
    chat = {r["role"]: r["provenance"] for r in _records(archive, "coaching/chat_messages.json")}
    assert chat["user"]["kind"] == "user_entered"
    assert chat["assistant"]["kind"] == "derived"
    assert chat["assistant"]["authoritative"] is False


def test_imported_data_names_its_source_and_the_ledger_keeps_corrections(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    _seed(db, a, "A")
    archive = _open(db, a, _export(db, a)["export_id"])

    metric = _records(archive, "metrics/body_metrics.json")[0]
    assert metric["provenance"]["kind"] == "user_entered"
    assert metric["provenance"]["corrected"] is True
    health = _records(archive, "metrics/health_metrics.json")[0]
    assert (health["provenance"]["kind"], health["provenance"]["source"]) == ("imported", "oura")
    assert health["provenance"]["transformation_version"] == "2"

    ledger_rows = [
        r for r in _records(archive, "provenance/record_provenance.json") if r["record_type"] == "body_metrics"
    ]
    assert [(r["revision"], r["kind"], r["supersedes_revision"]) for r in ledger_rows] == [
        (1, "imported", None),
        (2, "user_entered", 1),
    ]
    assert ledger_rows[1]["lineage_note"] == "reason-A"
    assert ledger_rows[0]["retained_content"]  # the value the correction replaced
    assert all("id" not in row and "supersedes_id" not in row for row in ledger_rows)


def test_a_record_stored_before_provenance_tracking_says_so(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO daily_log (date, day, focus, carb_tier, plan, lifestyle, user_id) "
            "VALUES ('2025-12-01', 1, 'Legs', 'low', 'old plan', 'x', ?)",
            (a,),
        )
    record = _records(_open(db, a, _export(db, a)["export_id"]), "coaching/daily_plans.json")[0]
    assert record["provenance"] is None


def test_large_accounts_are_exported_completely_in_bounded_batches(
    db: str, users: tuple[str, str]
) -> None:
    a, b = users
    total = ae._BATCH_SIZE * 2 + 37
    with sqlite3.connect(db) as conn:
        for owner, count in ((a, total), (b, 5)):
            conn.executemany(
                "INSERT INTO notifications (user_id, type, title, message, created_at) "
                "VALUES (?, 'system', ?, 'm', ?)",
                [(owner, f"n{index:05d}", f"2026-01-01T00:00:{index % 60:02d}+00:00") for index in range(count)],
            )
            conn.executemany(
                "INSERT INTO chat_messages (role, content, created_at, user_id) VALUES ('user', ?, ?, ?)",
                [(f"c{index:05d}", f"2026-01-01T00:00:00.{index:06d}+00:00", owner) for index in range(count)],
            )
    archive = _open(db, a, _export(db, a)["export_id"])
    titles = [r["title"] for r in _records(archive, "notifications/notifications.json")]
    assert len(titles) == total
    assert len(set(titles)) == total
    chat = [r["content"] for r in _records(archive, "coaching/chat_messages.json")]
    assert chat == [f"c{index:05d}" for index in range(total)]  # stable order across batches


def test_ledger_lookups_cover_more_keys_than_one_query_chunk(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    count = 430  # beyond provenance_ledger._IN_CHUNK
    for index in range(count):
        save_daily_log(
            f"2025-{(index // 28) + 1:02d}-{(index % 28) + 1:02d}",
            1,
            "Legs",
            "high",
            "plan",
            "life",
            db,
            user_id=a,
            provenance=Provenance.derived(_AI, "chunk/1"),
        )
    records = _records(_open(db, a, _export(db, a)["export_id"]), "coaching/daily_plans.json")
    assert len(records) == count
    assert all(r["provenance"]["transformation_version"] == "chunk/1" for r in records)


def test_an_export_over_the_size_limit_fails_for_good_without_publishing(
    db: str, users: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = users
    monkeypatch.setenv("ACCOUNT_EXPORT_MAX_MB", "1")
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO notifications (user_id, type, title, message, created_at) "
            "VALUES (?, 'system', 't', ?, '2026-01-01T00:00:00+00:00')",
            [(a, "x" * 2000 + str(index)) for index in range(700)],
        )
    status, _ = ae.request_export(db, a, now=NOW)
    ae.process_due_exports(db, now=NOW)

    failed = ae.get_export(db, a, status["export_id"], now=NOW)
    assert failed is not None
    assert (failed["state"], failed["error_code"], failed["attempts"]) == ("failed", "too_large", 1)
    directory = ad.export_directory(db, a)
    assert directory is not None and list(directory.iterdir()) == []
    with pytest.raises(ae.ExportError) as caught:
        ae.open_download(db, a, status["export_id"], now=NOW)
    assert caught.value.code == "not_ready"
    ae.process_due_exports(db, now=NOW + timedelta(hours=1))  # never retried
    assert ae.get_export(db, a, status["export_id"], now=NOW)["attempts"] == 1  # type: ignore[index]


def test_the_owners_settings_travel_with_the_archive(db: str, users: tuple[str, str]) -> None:
    a, _ = users
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE users SET timezone = 'Europe/London', units = 'imperial' WHERE id = ?", (a,))
    manifest = json.loads(_open(db, a, _export(db, a)["export_id"]).read(f"{ROOT}/manifest.json"))
    assert manifest["preferences"]["timezone"] == "Europe/London"
    assert manifest["preferences"]["unit_system"] == "imperial"
    assert manifest["preferences"]["stored_weight_unit"] == "kg"


# ---------------------------------------------------------------------------
# Requesting: one at a time, bounded per day, owner only
# ---------------------------------------------------------------------------


def test_a_request_is_queued_and_a_second_one_returns_the_open_request(
    db: str, users: tuple[str, str]
) -> None:
    a, b = users
    first, created = ae.request_export(db, a, now=NOW)
    assert created is True
    assert (first["state"], first["can_download"], first["attempts"]) == ("queued", False, 0)
    assert ae.is_export_id(first["export_id"])

    again, created_again = ae.request_export(db, a, now=NOW + timedelta(seconds=5))
    assert created_again is False
    assert again["export_id"] == first["export_id"]
    assert len(_rows(db, "SELECT 1 FROM account_export_requests WHERE user_id = ?", a)) == 1

    other, created_other = ae.request_export(db, b, now=NOW)
    assert created_other is True and other["export_id"] != first["export_id"]


def test_requests_are_limited_per_rolling_day(
    db: str, users: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = users
    monkeypatch.setenv("ACCOUNT_EXPORT_DAILY_LIMIT", "2")
    _export(db, a, now=NOW)
    _export(db, a, now=NOW + timedelta(hours=1))
    with pytest.raises(ae.ExportError) as caught:
        ae.request_export(db, a, now=NOW + timedelta(hours=2))
    assert caught.value.code == "rate_limited"
    info = ae.describe_exports(db, a, now=NOW + timedelta(hours=2))
    assert (info["daily_limit"], info["remaining_today"]) == (2, 0)
    # The window rolls: once the first request is a day old a slot is free again.
    assert ae.request_export(db, a, now=NOW + timedelta(hours=25))[1] is True


def test_only_active_accounts_in_good_standing_can_request(db: str, users: tuple[str, str]) -> None:
    a, _ = users
    with pytest.raises(ae.ExportError) as unknown:
        ae.request_export(db, "no-such-account", now=NOW)
    assert unknown.value.code == "not_eligible"
    with pytest.raises(ae.ExportError) as blank:
        ae.request_export(db, "", now=NOW)
    assert blank.value.code == "not_eligible"
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE users SET status = 'suspended' WHERE id = ?", (a,))
    with pytest.raises(ae.ExportError) as suspended:
        ae.request_export(db, a, now=NOW)
    assert suspended.value.code == "not_eligible"
    assert ae.describe_exports(db, a, now=NOW)["eligible"] is False


def test_configuration_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (ae.retention_hours(), ae.daily_limit(), ae.max_bytes()) == (24, 5, 512 * 1024 * 1024)
    monkeypatch.setenv("ACCOUNT_EXPORT_RETENTION_HOURS", "9999")
    monkeypatch.setenv("ACCOUNT_EXPORT_DAILY_LIMIT", "nonsense")
    monkeypatch.setenv("ACCOUNT_EXPORT_MAX_MB", "0")
    assert ae.retention_hours() == ae.MAX_RETENTION_HOURS
    assert ae.daily_limit() == ae.DEFAULT_DAILY_LIMIT
    assert ae.max_bytes() == ae.MIN_MAX_MEGABYTES * 1024 * 1024


def test_get_export_is_identical_for_unknown_malformed_and_foreign_ids(
    db: str, users: tuple[str, str]
) -> None:
    a, b = users
    status = _export(db, a)
    assert ae.get_export(db, a, status["export_id"], now=NOW) is not None
    for candidate in (status["export_id"], "0" * 32, "../" + "a" * 29, status["export_id"].upper(), ""):
        assert ae.get_export(db, b, candidate, now=NOW) is None
    with pytest.raises(ae.ExportError) as caught:
        ae.open_download(db, b, status["export_id"], now=NOW)
    assert caught.value.code == "not_found"
    assert ae.describe_exports(db, b, now=NOW)["exports"] == []


# ---------------------------------------------------------------------------
# Executing: asynchronous, expiring, resumable and safe to repeat
# ---------------------------------------------------------------------------


def test_the_worker_publishes_a_private_expiring_archive(db: str, users: tuple[str, str]) -> None:
    a, _ = users
    _seed(db, a, "A")
    queued, _ = ae.request_export(db, a, now=NOW)
    assert ae.get_export(db, a, queued["export_id"], now=NOW)["state"] == "queued"  # type: ignore[index]
    with pytest.raises(ae.ExportError) as early:
        ae.open_download(db, a, queued["export_id"], now=NOW)
    assert early.value.code == "not_ready"

    assert ae.process_due_exports(db, now=NOW) == 1
    ready = ae.get_export(db, a, queued["export_id"], now=NOW)
    assert ready is not None
    assert ready["state"] == "ready" and ready["can_download"] is True
    assert ready["expires_at"] is not None and ready["completed_at"] is not None
    assert datetime.fromisoformat(ready["expires_at"]) - datetime.fromisoformat(
        ready["completed_at"]
    ) == timedelta(hours=24)

    target = ae.open_download(db, a, queued["export_id"], now=NOW)
    directory = ad.export_directory(db, a)
    assert directory is not None and target.path == directory / f"{queued['export_id']}.zip"
    assert target.size_bytes == target.path.stat().st_size == ready["size_bytes"]
    assert hashlib.sha256(target.path.read_bytes()).hexdigest() == ready["sha256"] == target.sha256
    assert stat.S_IMODE(target.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(directory.parent.stat().st_mode) == 0o700
    assert sorted(path.name for path in directory.iterdir()) == [target.path.name]  # no partials
    assert target.filename.startswith("workout-agent-export-20261112-")
    assert a not in target.filename and EMAIL_A not in target.filename


def test_retention_is_configurable_and_the_archive_expires(
    db: str, users: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = users
    monkeypatch.setenv("ACCOUNT_EXPORT_RETENTION_HOURS", "2")
    status = _export(db, a)
    path = ae.open_download(db, a, status["export_id"], now=NOW).path
    expires = datetime.fromisoformat(
        ae.get_export(db, a, status["export_id"], now=NOW)["expires_at"]  # type: ignore[index]
    )
    assert timedelta(0) <= expires - (NOW + timedelta(hours=2)) < timedelta(seconds=30)

    # Past its expiry the download is refused immediately, before any sweep ran...
    late = NOW + timedelta(hours=2, minutes=1)
    with pytest.raises(ae.ExportError) as refused:
        ae.open_download(db, a, status["export_id"], now=late)
    assert refused.value.code == "expired"
    assert ae.get_export(db, a, status["export_id"], now=late)["state"] == "expired"  # type: ignore[index]
    assert path.exists()
    # ...and the next worker tick removes the file and records the expiry.
    ae.process_due_exports(db, now=late)
    assert not path.exists()
    assert _rows(db, "SELECT state FROM account_export_requests WHERE id = ?", status["export_id"])[0][
        "state"
    ] == "expired"
    assert ae.expire_exports(db, now=late) == 0  # idempotent


def test_a_newer_export_replaces_the_older_archive(db: str, users: tuple[str, str]) -> None:
    a, _ = users
    first = _export(db, a, now=NOW)
    first_path = ae.open_download(db, a, first["export_id"], now=NOW).path
    second = _export(db, a, now=NOW + timedelta(minutes=10))

    assert ae.get_export(db, a, first["export_id"], now=NOW)["state"] == "expired"  # type: ignore[index]
    assert not first_path.exists()
    assert ae.open_download(db, a, second["export_id"], now=NOW).path.exists()
    directory = ad.export_directory(db, a)
    assert directory is not None and len(list(directory.iterdir())) == 1
    with pytest.raises(ae.ExportError) as gone:
        ae.open_download(db, a, first["export_id"], now=NOW)
    assert gone.value.code == "expired"


def test_the_worker_only_builds_for_the_proven_owner(db: str, users: tuple[str, str]) -> None:
    a, b = users
    queued, _ = ae.request_export(db, a, now=NOW)
    # B's identity is not A's request: nothing is claimed or built.
    assert ae.execute_export(db, queued["export_id"], b, now=NOW) is None
    assert _rows(db, "SELECT state, attempts FROM account_export_requests")[0]["attempts"] == 0
    assert ae.execute_export(db, "not-an-id", a, now=NOW) is None
    done = ae.execute_export(db, queued["export_id"], a, now=NOW)
    assert done is not None and done["state"] == "ready"
    # Repeating a finished request is a harmless no-op.
    assert ae.execute_export(db, queued["export_id"], a, now=NOW)["attempts"] == 1  # type: ignore[index]


def test_a_failed_attempt_is_retried_with_back_off_then_fails_for_good(
    db: str, users: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = users
    calls: list[str] = []

    def broken(*args: Any, **kwargs: Any) -> Any:
        calls.append("build")
        raise OSError("disk full")

    monkeypatch.setattr(ae, "build_archive", broken)
    status, _ = ae.request_export(db, a, now=NOW)
    export_id = status["export_id"]
    directory = ad.export_directory(db, a)
    assert directory is not None

    ae.process_due_exports(db, now=NOW)
    row = ae.get_export(db, a, export_id, now=NOW)
    assert row is not None and (row["state"], row["error_code"], row["attempts"]) == (
        "queued",
        "filesystem_error",
        1,
    )
    assert list(directory.iterdir()) == []  # the partial file is cleaned up
    ae.process_due_exports(db, now=NOW + timedelta(minutes=1))  # still backing off
    assert len(calls) == 1
    ae.process_due_exports(db, now=NOW + ae.RETRY_BACKOFF + timedelta(seconds=1))
    assert len(calls) == 2
    ae.process_due_exports(db, now=NOW + 3 * ae.RETRY_BACKOFF)
    final = ae.get_export(db, a, export_id, now=NOW)
    assert final is not None and (final["state"], final["attempts"]) == ("failed", ae.MAX_ATTEMPTS)
    assert len(calls) == ae.MAX_ATTEMPTS
    ae.process_due_exports(db, now=NOW + timedelta(days=1))
    assert len(calls) == ae.MAX_ATTEMPTS  # a failed export is never retried again
    # The account can ask again; the new request is not blocked by the failed one.
    monkeypatch.undo()
    assert ae.request_export(db, a, now=NOW + timedelta(days=1))[1] is True


def test_a_crashed_worker_is_resumed_once_its_lease_expires(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    status, _ = ae.request_export(db, a, now=NOW)
    assert ae._claim(db, status["export_id"], a, NOW) == 1  # a worker takes it, then dies
    ae.process_due_exports(db, now=NOW + timedelta(minutes=5))  # lease still held
    assert ae.get_export(db, a, status["export_id"], now=NOW)["state"] == "running"  # type: ignore[index]
    later = NOW + ae.LEASE + timedelta(seconds=1)
    assert ae.process_due_exports(db, now=later) == 1
    resumed = ae.get_export(db, a, status["export_id"], now=later)
    assert resumed is not None and (resumed["state"], resumed["attempts"]) == ("ready", 2)


def test_a_stalled_final_attempt_is_failed_and_cleaned_up(db: str, users: tuple[str, str]) -> None:
    a, _ = users
    status, _ = ae.request_export(db, a, now=NOW)
    moment = NOW
    for _ in range(ae.MAX_ATTEMPTS):  # every attempt dies mid-build
        assert ae._claim(db, status["export_id"], a, moment) is not None
        moment += ae.LEASE + timedelta(seconds=1)
    directory = ad.export_directory(db, a)
    assert directory is not None
    directory.mkdir(parents=True)
    leftover = directory / f".{status['export_id']}.{ae.MAX_ATTEMPTS}.partial"
    leftover.write_bytes(b"half-written")
    ae.process_due_exports(db, now=moment)
    stalled = ae.get_export(db, a, status["export_id"], now=moment)
    assert stalled is not None and (stalled["state"], stalled["error_code"]) == ("failed", "timed_out")
    assert not leftover.exists()
    assert ae.request_export(db, a, now=moment)[1] is True  # no longer blocked


def test_a_superseded_attempt_can_neither_publish_nor_settle(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    status, _ = ae.request_export(db, a, now=NOW)
    export_id = status["export_id"]
    assert ae._claim(db, export_id, a, NOW) == 1
    later = NOW + ae.LEASE + timedelta(seconds=1)
    assert ae._claim(db, export_id, a, later) == 2  # the lease lapsed; attempt 2 owns it

    with pytest.raises(ae._Aborted):  # attempt 1 finishes its build late...
        ae._build_and_publish(db, export_id, a, 1, ae._Clock(NOW))
    ae._settle_failure(db, export_id, a, 1, "filesystem_error", later)  # ...or fails late
    row = _rows(db, "SELECT state, attempts, error_code FROM account_export_requests")[0]
    assert (row["state"], row["attempts"], row["error_code"]) == ("running", 2, None)
    directory = ad.export_directory(db, a)
    assert directory is not None and not list(directory.glob("*.1.partial"))
    done = ae.execute_export(db, export_id, a, now=later + ae.LEASE + timedelta(seconds=1))
    assert done is not None and done["state"] == "ready"


def test_a_long_build_renews_its_lease_and_stops_when_it_is_lost(
    db: str, users: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = users
    status, _ = ae.request_export(db, a, now=NOW)
    export_id = status["export_id"]
    assert ae._claim(db, export_id, a, NOW) == 1
    monkeypatch.setattr(ae, "HEARTBEAT_INTERVAL", 0.0)

    beat = ae._Heartbeat(db, export_id, a, 1, ae._Clock(NOW))
    beat()
    renewed = _rows(db, "SELECT next_attempt_at FROM account_export_requests")[0][0]
    assert renewed > (NOW + timedelta(minutes=14)).isoformat()

    with sqlite3.connect(db) as conn:  # the account is suspended mid-build
        conn.execute("UPDATE users SET status = 'suspended' WHERE id = ?", (a,))
    with pytest.raises(ae.ExportError) as stopped:
        beat()
    assert stopped.value.code == "not_eligible"

    with sqlite3.connect(db) as conn:  # a later attempt takes over
        conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (a,))
        conn.execute("UPDATE account_export_requests SET attempts = 2")
    with pytest.raises(ae._Aborted):
        beat()


def test_the_heartbeat_is_throttled(db: str, users: tuple[str, str]) -> None:
    a, _ = users
    status, _ = ae.request_export(db, a, now=NOW)
    assert ae._claim(db, status["export_id"], a, NOW) == 1
    before = _rows(db, "SELECT next_attempt_at FROM account_export_requests")[0][0]
    beat = ae._Heartbeat(db, status["export_id"], a, 1, ae._Clock(NOW + timedelta(hours=1)))
    for _ in range(50):
        beat()
    assert _rows(db, "SELECT next_attempt_at FROM account_export_requests")[0][0] == before


# ---------------------------------------------------------------------------
# Account deletion and closure fence exports
# ---------------------------------------------------------------------------


def test_an_export_can_be_made_while_account_closure_is_pending(
    db: str, users: tuple[str, str]
) -> None:
    """The cancellation window is exactly when someone wants their data."""
    a, _ = users
    _seed(db, a, "A")
    ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
    assert ad.account_gate(db, a) is ad.AccountGate.PENDING
    done = _export(db, a)
    assert done["state"] == "ready"
    states = {r["state"] for r in _records(_open(db, a, done["export_id"]), "audit/deletion_requests.json")}
    assert states == {"pending", "cancelled"}


def test_exports_are_refused_while_erasure_runs(db: str, users: tuple[str, str]) -> None:
    a, _ = users
    queued, _ = ae.request_export(db, a, now=NOW)  # asked for before the closure
    ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
    with sqlite3.connect(db) as conn:  # erasure starts: the account is locked
        conn.execute("UPDATE account_deletion_requests SET state = 'in_progress' WHERE user_id = ?", (a,))
    assert ad.account_gate(db, a) is ad.AccountGate.LOCKED
    with pytest.raises(ae.ExportError) as new_request:
        ae.request_export(db, a, now=NOW + timedelta(minutes=1))
    assert new_request.value.code == "not_eligible"

    ae.process_due_exports(db, now=NOW + timedelta(minutes=2))  # the queued one is not built
    refused = ae.get_export(db, a, queued["export_id"], now=NOW)
    assert refused is not None and (refused["state"], refused["error_code"]) == ("failed", "not_eligible")
    directory = ad.export_directory(db, a)
    assert directory is None or not directory.exists() or list(directory.iterdir()) == []


def test_an_export_never_publishes_for_an_account_claimed_for_erasure_mid_build(
    db: str, users: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = users
    real = ae.build_archive

    def build_then_lock(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        monkeypatch.setenv("ACCOUNT_DELETION_GRACE_DAYS", "0")
        request = ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
        assert ad._claim(db, request["request_id"], NOW) is not None  # erasure claims the account
        return result

    monkeypatch.setattr(ae, "build_archive", build_then_lock)
    status, _ = ae.request_export(db, a, now=NOW)
    ae.process_due_exports(db, now=NOW)

    row = _rows(db, "SELECT state, error_code FROM account_export_requests WHERE id = ?", status["export_id"])[0]
    assert (row["state"], row["error_code"]) == ("failed", "not_eligible")
    directory = ad.export_directory(db, a)
    assert directory is not None and list(directory.iterdir()) == []  # nothing left behind


def test_closing_the_account_erases_export_rows_and_files(
    db: str, users: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = users
    monkeypatch.setenv("ACCOUNT_DELETION_GRACE_DAYS", "0")
    mine = _export(db, a)
    theirs = _export(db, b)
    path_a = ae.open_download(db, a, mine["export_id"], now=NOW).path
    path_b = ae.open_download(db, b, theirs["export_id"], now=NOW).path

    request = ad.request_deletion(db, a, "account", EMAIL_A, now=NOW)
    status = ad.execute_deletion(db, request["request_id"], now=NOW)
    assert status is not None and status["state"] == "completed"

    assert not path_a.exists() and not path_a.parent.exists()
    assert _rows(db, "SELECT 1 FROM account_export_requests WHERE user_id = ?", a) == []
    assert path_b.exists()
    assert ae.open_download(db, b, theirs["export_id"], now=NOW).path == path_b


def test_erasing_health_data_removes_the_archives_that_embed_it(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    done = _export(db, a)
    path = ae.open_download(db, a, done["export_id"], now=NOW).path
    request = ad.request_deletion(db, a, "health_data", ad.HEALTH_CONFIRMATION, now=NOW)
    ad.execute_deletion(db, request["request_id"], now=NOW)
    assert not path.exists()
    assert _rows(db, "SELECT 1 FROM account_export_requests WHERE user_id = ?", a) == []
    assert _rows(db, "SELECT 1 FROM users WHERE id = ?", a)  # the account itself stays


# ---------------------------------------------------------------------------
# Files on disk: never trust a path
# ---------------------------------------------------------------------------


def test_a_tampered_or_swapped_file_is_never_served(db: str, users: tuple[str, str]) -> None:
    a, _ = users
    done = _export(db, a)
    path = ae.open_download(db, a, done["export_id"], now=NOW).path
    original = path.read_bytes()

    path.write_bytes(original + b"tail")  # size no longer matches the recorded one
    with pytest.raises(ae.ExportError) as resized:
        ae.open_download(db, a, done["export_id"], now=NOW)
    assert resized.value.code == "expired"

    path.unlink()
    outside = path.parent.parent / "outside.zip"
    outside.write_bytes(original)
    path.symlink_to(outside)  # a symlink is never followed
    with pytest.raises(ae.ExportError) as linked:
        ae.open_download(db, a, done["export_id"], now=NOW)
    assert linked.value.code == "expired"

    path.unlink()
    with pytest.raises(ae.ExportError) as missing:
        ae.open_download(db, a, done["export_id"], now=NOW)
    assert missing.value.code == "expired"


def test_a_symlinked_export_directory_fails_closed(db: str, users: tuple[str, str]) -> None:
    a, _ = users
    root = Path(db).resolve().parent / ad.EXPORT_DIRNAME
    elsewhere = Path(db).resolve().parent / "elsewhere"
    elsewhere.mkdir()
    root.symlink_to(elsewhere, target_is_directory=True)
    status, _ = ae.request_export(db, a, now=NOW)
    ae.process_due_exports(db, now=NOW)
    failed = ae.get_export(db, a, status["export_id"], now=NOW)
    assert failed is not None and (failed["state"], failed["error_code"]) == ("failed", "unsafe_directory")
    assert list(elsewhere.iterdir()) == []


def test_unsafe_owner_ids_never_map_to_a_path(db: str) -> None:
    for hostile in ("../escape", "a/b", "..", ""):
        assert ae._artifact(db, hostile, "0" * 32) is None
        assert ae._partial(db, hostile, "0" * 32, 1) is None
    assert ae._artifact(db, "user-1", "../../etc/passwd") is None


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def test_the_scheduler_builds_queued_exports(db: str, users: tuple[str, str]) -> None:
    a, _ = users
    status, _ = ae.request_export(db, a)
    assert scheduler.run_data_exports(SimpleNamespace(database_path=db)) == 1  # type: ignore[arg-type]
    done = ae.get_export(db, a, status["export_id"])
    assert done is not None and done["state"] == "ready"


def test_a_failing_export_tick_never_stops_the_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(db_path: str) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(scheduler, "process_due_exports", explode)
    assert scheduler.run_data_exports(SimpleNamespace(database_path="x")) == 0  # type: ignore[arg-type]


def test_a_database_without_the_export_table_is_left_alone(tmp_path: Path) -> None:
    path = str(tmp_path / "old.db")
    sqlite3.connect(path).close()
    assert ae.process_due_exports(path) == 0


def test_run_once_processes_exports_before_coaching(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(scheduler, "run_account_deletions", lambda config: order.append("deletions"))
    monkeypatch.setattr(scheduler, "run_data_exports", lambda config: order.append("exports"))
    monkeypatch.setattr(scheduler, "run_coaching", lambda config: order.append("coaching") or 0)
    monkeypatch.setattr(scheduler, "run_daily_insight", lambda config: order.append("insight"))
    monkeypatch.setattr(scheduler, "get_all_users", lambda path=None: [])
    assert scheduler.run_once(SimpleNamespace(database_path="x")) == 0  # type: ignore[arg-type]
    assert order[:3] == ["deletions", "exports", "coaching"]


def test_owner_jobs_are_reauthorized_when_they_run(db: str, users: tuple[str, str]) -> None:
    """A request whose owner no longer exists is failed, not resurrected."""
    a, _ = users
    status, _ = ae.request_export(db, a, now=NOW)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (a,))
    assert ae.process_due_exports(db, now=NOW) == 0
    row = _rows(db, "SELECT state, error_code FROM account_export_requests WHERE id = ?", status["export_id"])[0]
    assert (row["state"], row["error_code"]) == ("failed", "not_eligible")
    exports = Path(db).parent / ad.EXPORT_DIRNAME
    assert not exports.exists() or not any(exports.rglob("*.zip"))


def test_user_id_is_immutable_on_export_requests(db: str, users: tuple[str, str]) -> None:
    a, b = users
    ae.request_export(db, a, now=NOW)
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE account_export_requests SET user_id = ?", (b,))


def test_old_finished_requests_are_forgotten_but_recent_and_ready_ones_stay(
    db: str, users: tuple[str, str]
) -> None:
    a, _ = users
    stale = _export(db, a, now=NOW)  # ready now, expired and old by the end
    recent_failed, _ = ae.request_export(db, a, now=NOW + timedelta(days=29))
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE account_export_requests SET state = 'failed' WHERE id = ?", (recent_failed["export_id"],))
    ae.process_due_exports(db, now=NOW + timedelta(days=29, hours=1))
    assert ae.get_export(db, a, stale["export_id"], now=NOW) is not None  # expired, 29 days old

    ae.process_due_exports(db, now=NOW + timedelta(days=31))
    assert ae.get_export(db, a, stale["export_id"], now=NOW) is None
    assert ae.get_export(db, a, recent_failed["export_id"], now=NOW) is not None
    current = _export(db, a, now=NOW + timedelta(days=32))  # a ready export is never pruned
    ae.process_due_exports(db, now=NOW + timedelta(days=32, hours=1))
    assert ae.get_export(db, a, current["export_id"], now=NOW + timedelta(days=32, hours=1)) is not None
