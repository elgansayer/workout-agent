"""Drift tests for Workout Agent canonical programme, seeds, runtime fixtures, and docs.

Verifies:
1. One canonical programme definition produces live pages, seeded data, exports, and docs.
2. A migration identifies which existing active programme version is authoritative and records the decision.
3. A drift test fails when README examples, seeds, and runtime fixtures diverge unintentionally.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import database
import pytest
from canonical_programme import (
    CANONICAL_CHEST_BACK_EXERCISES,
    CANONICAL_CYCLE_WEEKS,
    CANONICAL_GOAL,
    CANONICAL_LEGS_TRUNK_EXERCISES,
    CANONICAL_PROGRAMME_ID,
    CANONICAL_SHOULDERS_ARMS_EXERCISES,
    CANONICAL_SPLIT_NAME,
    CANONICAL_TEMPLATE_KEY,
    export_canonical_workout,
    get_canonical_programme_definition,
    get_canonical_training_data,
    render_documentation_example,
)
from itsdangerous import TimestampSigner
from starlette.testclient import TestClient

_SECRET = "drift-test-secret-key-that-is-sufficiently-long"


def _session_cookie(user_id: str) -> str:
    payload = base64.b64encode(
        json.dumps({"user": "athlete@example.test", "user_id": user_id}).encode()
    )
    return TimestampSigner(_SECRET).sign(payload).decode()


def _parse_doc_exercises(text: str) -> dict[str, list[dict[str, str]]]:
    """Parse exercise bullets grouped by day focus heading from markdown text."""
    pattern = re.compile(r"^## Day [0-9]+ and [0-9]+:\s*(.+)$", re.MULTILINE)
    bullet_pattern = re.compile(r"^-\s*(.+?):\s*(\d+)\s*sets\s*x\s*([0-9-]+)\s*reps", re.MULTILINE)

    sections: dict[str, list[dict[str, str]]] = {}
    current_focus: str | None = None

    for line in text.splitlines():
        heading_match = pattern.match(line.strip())
        if heading_match:
            current_focus = heading_match.group(1).strip().lower()
            sections[current_focus] = []
            continue
        if current_focus:
            bullet_match = bullet_pattern.match(line.strip())
            if bullet_match:
                sections[current_focus].append(
                    {
                        "name": bullet_match.group(1).strip(),
                        "sets": bullet_match.group(2).strip(),
                        "rep_range": bullet_match.group(3).strip(),
                    }
                )
    return sections


def test_documentation_example_exact_drift() -> None:
    """current-workout.md must match render_documentation_example() exactly."""
    doc_path = Path("current-workout.md")
    assert doc_path.exists(), "current-workout.md must exist in repository root"
    doc_content = doc_path.read_text(encoding="utf-8")
    expected = render_documentation_example()
    assert doc_content == expected, (
        "current-workout.md has drifted from canonical_programme.render_documentation_example()"
    )


def test_documentation_exercises_drift_detector() -> None:
    """Parsed markdown exercise prescriptions must match canonical definitions."""
    doc_content = Path("current-workout.md").read_text(encoding="utf-8")
    parsed = _parse_doc_exercises(doc_content)
    canonical = get_canonical_programme_definition()

    # Verify all 3 routines are present
    assert "chest and back" in parsed
    assert "shoulders and arms" in parsed
    assert "legs and trunk" in parsed

    # Verify counts
    assert len(parsed["chest and back"]) == len(CANONICAL_CHEST_BACK_EXERCISES) == 5
    assert len(parsed["shoulders and arms"]) == len(CANONICAL_SHOULDERS_ARMS_EXERCISES) == 5
    assert len(parsed["legs and trunk"]) == len(CANONICAL_LEGS_TRUNK_EXERCISES) == 7

    # Verify exercise details match canonical definition Block 1 prescriptions
    for day in canonical["days"]:
        focus_key = day["focus"].strip().lower()
        doc_exercises = parsed[focus_key]
        for idx, ex in enumerate(day["exercises"]):
            assert doc_exercises[idx]["name"] == ex["name"], (
                f"Exercise name drift on {day['focus']}: {doc_exercises[idx]['name']} vs {ex['name']}"
            )
            assert int(doc_exercises[idx]["sets"]) == ex["sets"], (
                f"Set count drift on {ex['name']}: {doc_exercises[idx]['sets']} vs {ex['sets']}"
            )
            assert doc_exercises[idx]["rep_range"] == ex["rep_range"], (
                f"Rep range drift on {ex['name']}: {doc_exercises[idx]['rep_range']} vs {ex['rep_range']}"
            )


def test_drift_detector_fails_on_unintentional_divergence() -> None:
    """The drift detector must raise AssertionError when markdown text diverges."""
    tampered_md = render_documentation_example().replace("12-15 reps", "20-25 reps")
    parsed = _parse_doc_exercises(tampered_md)
    canonical = get_canonical_programme_definition()
    first_day = canonical["days"][0]
    first_ex = first_day["exercises"][0]

    with pytest.raises(AssertionError, match="Rep range drift"):
        doc_exercises = parsed["chest and back"]
        assert doc_exercises[0]["rep_range"] == first_ex["rep_range"], (
            f"Rep range drift on {first_ex['name']}: {doc_exercises[0]['rep_range']} vs {first_ex['rep_range']}"
        )


def test_seeded_data_matches_canonical(tmp_path: Path) -> None:
    """Seeded canonical programme matches canonical definition and records decision."""
    db_file = str(tmp_path / "seeded.db")
    database.init_db(db_file)
    legacy_uid = database.get_legacy_user_id(db_file)

    prog_id = database.seed_canonical_programme(legacy_uid, db_file)
    assert prog_id > 0

    active = database.get_active_programme(legacy_uid, db_file)
    assert active is not None
    assert active["source"] == "hevy"
    assert active["template_key"] == CANONICAL_TEMPLATE_KEY
    assert active["active"] is True

    canonical = get_canonical_programme_definition()
    assert active["definition"]["name"] == canonical["name"]
    assert active["definition"]["cycle_weeks"] == canonical["cycle_weeks"]
    assert len(active["definition"]["days"]) == len(canonical["days"])
    assert len(active["definition"]["blocks"]) == len(canonical["blocks"])

    # Verify decision record
    decision = database.get_authoritative_programme_decision(legacy_uid, db_file)
    assert decision is not None
    assert decision["user_id"] == legacy_uid
    assert decision["authoritative_programme_id"] == prog_id
    assert decision["source"] == "hevy"
    assert decision["decision"] == "authoritative"
    assert decision["reason"] == "canonical_seed"

    # Verify programme_state split_name
    with database._connect(db_file) as conn:
        state = conn.execute(
            "SELECT split_name FROM programme_state WHERE user_id = ?",
            (legacy_uid,),
        ).fetchone()
        assert state[0] == CANONICAL_SPLIT_NAME


def test_runtime_fixtures_match_canonical() -> None:
    """Runtime training data fixture produces routines and exercises matching canonical spec."""
    training_data = get_canonical_training_data()
    assert len(training_data.routines) == 3
    assert training_data.routines[0].title == "Chest and back"
    assert training_data.routines[1].title == "Shoulders and arms"
    assert training_data.routines[2].title == "Legs and trunk"

    assert len(training_data.routines[0].exercises) == 5
    assert len(training_data.routines[1].exercises) == 5
    assert len(training_data.routines[2].exercises) == 7

    canonical = get_canonical_programme_definition()
    for routine_idx, routine in enumerate(training_data.routines):
        canonical_day = canonical["days"][routine_idx]
        assert routine.title == canonical_day["focus"]
        for ex_idx, ex in enumerate(routine.exercises):
            canonical_ex = canonical_day["exercises"][ex_idx]
            assert ex.title == canonical_ex["name"]
            assert ex.sets == canonical_ex["source_sets"]


def test_live_pages_produce_canonical_programme(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Live plan, dashboard, and settings API produce the canonical programme when seeded."""
    db_file = str(tmp_path / "live.db")
    database.init_db(db_file)
    legacy_uid = database.get_legacy_user_id(db_file)
    database.seed_canonical_programme(
        legacy_uid, db_file, start_date=date(2026, 9, 1)
    )

    monkeypatch.setenv("DATABASE_PATH", db_file)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEB_AUTH_SECRET", _SECRET)
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_ID", "live-client-id")
    monkeypatch.setenv("WEB_GOOGLE_CLIENT_SECRET", "live-client-secret")

    import importlib

    import webapp.app as app_module
    importlib.reload(app_module)
    monkeypatch.setattr(app_module, "ALLOW_ANONYMOUS_WEB", True)
    monkeypatch.setattr("programme_runtime.local_today", lambda tz: date(2026, 9, 1))

    client = TestClient(app_module.app)
    client.cookies.set("session", _session_cookie(legacy_uid))

    # Test Plan endpoint
    plan_resp = client.get("/api/plan")
    assert plan_resp.status_code == 200
    plan_data = plan_resp.json()
    assert plan_data["setup_required"] is False
    assert plan_data["is_active_programme"] is True
    assert plan_data["split_name"] == CANONICAL_SPLIT_NAME
    assert plan_data["cycle_weeks"] == CANONICAL_CYCLE_WEEKS
    assert len(plan_data["blocks"]) == 3
    assert len(plan_data["days"]) == 3
    assert plan_data["days"][0]["focus"] == "Chest and back"

    # Test Dashboard endpoint
    dash_resp = client.get("/api/dashboard")
    assert dash_resp.status_code == 200
    dash_data = dash_resp.json()
    assert dash_data["setup_required"] is False
    assert dash_data["focus"] == "Chest and back"
    assert len(dash_data["rows"]) == 5
    assert dash_data["rows"][0]["name"] == "Incline dumbbell fly"
    assert dash_data["rows"][0]["planned"] == "3 × 12-15"

    # Test Settings endpoint (configured profile)
    settings_resp = client.get("/api/settings")
    assert settings_resp.status_code == 200
    settings_data = settings_resp.json()
    assert settings_data["user_prefs"]["goals"] == [CANONICAL_GOAL]


def test_export_canonical_workout() -> None:
    """Workout export derives from canonical programme with tenant safety."""
    exported = export_canonical_workout(
        user_id=42,
        day_number=1,
        provider="garmin_training",
        explicit_user_approval=True,
    )
    assert exported.user_id == 42
    assert exported.name == "Chest and back"
    assert exported.source_programme_id == CANONICAL_PROGRAMME_ID
    assert exported.provider == "garmin_training"

    # Tenant verification
    exported.validate(authenticated_user_id=42)
    with pytest.raises(ValueError, match="cannot export a workout to another user"):
        exported.validate(authenticated_user_id=99)


def test_migration_identifies_authoritative_when_multiple_active(
    tmp_path: Path,
) -> None:
    """Migration deactivates competing programmes, chooses authoritative, and records decision."""
    db_file = str(tmp_path / "multi_active.db")
    database.init_db(db_file)
    user_a = database.get_or_create_user("user_a@example.test", db_path=db_file)
    uid = user_a["id"]

    canonical_def = get_canonical_programme_definition()
    legacy_template_def = {
        "name": "Old Template",
        "blocks": [],
        "days": [],
    }

    # Insert two active programmes: an older template and a newer hevy programme
    with database._connect(db_file) as conn:
        conn.execute(
            """
            INSERT INTO programmes (user_id, source, template_key, definition, active, created_at, updated_at)
            VALUES (?, 'template', 'old-template', ?, 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """,
            (uid, json.dumps(legacy_template_def)),
        )
        template_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        conn.execute(
            """
            INSERT INTO programmes (user_id, source, template_key, definition, active, created_at, updated_at)
            VALUES (?, 'hevy', 'canonical-hevy', ?, 1, '2026-06-01T00:00:00Z', '2026-06-01T00:00:00Z')
            """,
            (uid, json.dumps(canonical_def)),
        )
        hevy_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Verify both were active before migration
    with database._connect(db_file) as conn:
        actives = conn.execute(
            "SELECT id FROM programmes WHERE user_id = ? AND active = 1", (uid,)
        ).fetchall()
        assert len(actives) == 2

    # Run migration via init_db
    database.init_db(db_file)

    # Verify only the Hevy programme remains active
    with database._connect(db_file) as conn:
        active_rows = conn.execute(
            "SELECT id, source, template_key, active FROM programmes WHERE user_id = ? AND active = 1",
            (uid,),
        ).fetchall()
        assert len(active_rows) == 1
        assert active_rows[0][0] == hevy_id
        assert active_rows[0][1] == "hevy"

        # Old template must be deactivated
        old_status = conn.execute(
            "SELECT active FROM programmes WHERE id = ?", (template_id,)
        ).fetchone()
        assert old_status[0] == 0

    # Verify the decision was recorded in programme_decisions
    decision = database.get_authoritative_programme_decision(uid, db_file)
    assert decision is not None
    assert decision["user_id"] == uid
    assert decision["authoritative_programme_id"] == hevy_id
    assert decision["source"] == "hevy"
    assert decision["reason"] == "reconciled_multiple_active"


def test_migration_records_decision_for_single_active(tmp_path: Path) -> None:
    """Migration records the decision for an existing single active programme."""
    db_file = str(tmp_path / "single_active.db")
    database.init_db(db_file)
    user = database.get_or_create_user("user_b@example.test", db_path=db_file)
    uid = user["id"]

    canonical_def = get_canonical_programme_definition()
    with database._connect(db_file) as conn:
        conn.execute(
            """
            INSERT INTO programmes (user_id, source, template_key, definition, active, created_at, updated_at)
            VALUES (?, 'hevy', 'single-key', ?, 1, '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')
            """,
            (uid, json.dumps(canonical_def)),
        )
        prog_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    database.init_db(db_file)

    decision = database.get_authoritative_programme_decision(uid, db_file)
    assert decision is not None
    assert decision["user_id"] == uid
    assert decision["authoritative_programme_id"] == prog_id
    assert decision["reason"] == "single_active"


def test_migration_multi_tenant_isolation(tmp_path: Path) -> None:
    """Authoritative decisions remain strictly isolated between tenants."""
    db_file = str(tmp_path / "tenants.db")
    database.init_db(db_file)
    user1 = database.get_or_create_user("tenant1@example.test", db_path=db_file)
    user2 = database.get_or_create_user("tenant2@example.test", db_path=db_file)

    canonical_def = get_canonical_programme_definition()
    with database._connect(db_file) as conn:
        conn.execute(
            """
            INSERT INTO programmes (user_id, source, template_key, definition, active, created_at, updated_at)
            VALUES (?, 'hevy', 't1-key', ?, 1, '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')
            """,
            (user1["id"], json.dumps(canonical_def)),
        )
        t1_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        conn.execute(
            """
            INSERT INTO programmes (user_id, source, template_key, definition, active, created_at, updated_at)
            VALUES (?, 'hevy', 't2-key', ?, 1, '2026-08-02T00:00:00Z', '2026-08-02T00:00:00Z')
            """,
            (user2["id"], json.dumps(canonical_def)),
        )
        t2_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    database.init_db(db_file)

    d1 = database.get_authoritative_programme_decision(user1["id"], db_file)
    d2 = database.get_authoritative_programme_decision(user2["id"], db_file)

    assert d1 is not None and d2 is not None
    assert d1["user_id"] == user1["id"]
    assert d1["authoritative_programme_id"] == t1_id
    assert d2["user_id"] == user2["id"]
    assert d2["authoritative_programme_id"] == t2_id
    assert d1["authoritative_programme_id"] != d2["authoritative_programme_id"]
