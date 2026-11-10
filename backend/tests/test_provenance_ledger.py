"""Persistence tests for record provenance, lineage and corrections."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

import provenance_ledger as ledger
import pytest
from database import (
    correct_body_metrics,
    get_body_metrics,
    get_daily_logs,
    get_or_create_user,
    get_provenance_summary,
    get_record_lineage,
    get_record_provenance,
    init_db,
    save_body_metrics,
    save_checkin,
    save_daily_log,
    save_dashboard_insight,
    save_deep_correlation,
    save_progress,
    save_reasoning_log,
    save_workout,
)
from hevy_parser import ExerciseSummary, WorkoutSummary, parse_workout
from provenance import (
    MANUAL_SOURCE,
    UNATTRIBUTED_SOURCE,
    Provenance,
    RecordType,
)

NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _db(tmp_path: Any) -> str:
    path = str(tmp_path / "provenance.db")
    init_db(path)
    return path


def _users(db: str) -> tuple[str, str]:
    a = get_or_create_user("a@example.com", "A", db_path=db)["id"]
    b = get_or_create_user("b@example.com", "B", db_path=db)["id"]
    return a, b


def _imported(source: str = "google_health", **kw: Any) -> Provenance:
    return Provenance.imported(source, "body/1", ingested_at=NOW, **kw)


def _ai(version: str = "daily-plan/1") -> Provenance:
    return Provenance.derived("ai:gemini/test", version, ingested_at=NOW)


def _rows(db: str, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db) as conn:
        return conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# Schema and migration safety
# ---------------------------------------------------------------------------


def test_ledger_schema_is_additive_idempotent_and_tenant_indexed(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    save_body_metrics({"weight_kg": 80.0}, "2026-01-01", db, user_id=user, provenance=_imported())
    before = _rows(db, "SELECT * FROM record_provenance")

    init_db(db)
    init_db(db)

    assert _rows(db, "SELECT * FROM record_provenance") == before
    foreign_keys = _rows(db, "PRAGMA foreign_key_list(record_provenance)")
    assert any(row[2] == "users" and row[3] == "user_id" for row in foreign_keys)
    first_columns = {
        _rows(db, f"SELECT name FROM pragma_index_info('{name}') WHERE seqno = 0")[0][0]
        for (name,) in _rows(db, "SELECT name FROM pragma_index_list('record_provenance')")
    }
    assert first_columns == {"user_id"}


def test_ledger_table_is_recreated_for_a_database_created_before_it_existed(
    tmp_path: Any,
) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    with sqlite3.connect(db) as conn:  # a reading stored by the previous release
        conn.execute(
            "INSERT INTO body_metrics (date, weight_kg, user_id) VALUES ('2026-01-01', 80.0, ?)",
            (user,),
        )
        conn.execute("DROP TABLE record_provenance")

    init_db(db)

    assert _rows(db, "SELECT COUNT(*) FROM body_metrics") == [(1,)]
    assert get_record_provenance(user, RecordType.BODY_METRIC, "2026-01-01", db) is None
    reading = get_body_metrics(db_path=db, user_id=user, include_provenance=True)[0]
    assert reading["weight_kg"] == 80.0 and reading["provenance"] is None


def test_ledger_rows_cannot_be_rewritten(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    save_body_metrics({"weight_kg": 80.0}, "2026-01-01", db, user_id=user, provenance=_imported())
    with sqlite3.connect(db) as conn:
        for statement in (
            "UPDATE record_provenance SET source = 'manual'",
            "UPDATE record_provenance SET user_id = 'someone-else'",
            "UPDATE record_provenance SET retained_content = '{}'",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                conn.execute(statement)


def test_ledger_enforces_kind_and_confidence_constraints(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    with sqlite3.connect(db) as conn:
        insert = (
            "INSERT INTO record_provenance (user_id, record_type, record_key, revision, "
            "kind, source, ingested_at, transformation_version, confidence, content_hash) "
            "VALUES (?, 'body_metrics', 'k', 1, ?, 'hevy', 't', 'v', ?, 'h')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (user, "invented", None))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (user, "imported", 1.5))


# ---------------------------------------------------------------------------
# Ledger semantics
# ---------------------------------------------------------------------------


def test_revisions_chain_and_identical_reimports_add_no_history(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    prov = _imported(external_id="ext-1", source_timestamp="2026-01-01T07:00:00Z", confidence=0.8)
    with sqlite3.connect(db) as conn:
        first = ledger.record_revision(
            conn, user_id=user, record_type=RecordType.BODY_METRIC, record_key="k",
            provenance=prov, content={"w": 1},
        )
        same = ledger.record_revision(
            conn, user_id=user, record_type=RecordType.BODY_METRIC, record_key="k",
            provenance=prov, content={"w": 1},
        )
        second = ledger.record_revision(
            conn, user_id=user, record_type=RecordType.BODY_METRIC, record_key="k",
            provenance=prov, content={"w": 2},
        )
        conn.commit()
        history = ledger.lineage(conn, user, RecordType.BODY_METRIC, "k")

    assert (first.status, first.revision) == ("recorded", 1)
    assert (same.status, same.entry_id) == ("unchanged", first.entry_id)
    assert (second.status, second.revision) == ("recorded", 2)
    assert [e.revision for e in history] == [2, 1]  # newest first
    newest, oldest = history
    assert newest.supersedes_id == oldest.id and newest.supersedes_revision == 1
    assert oldest.supersedes_id is None
    stored = oldest.provenance
    assert stored.source == "google_health"
    assert stored.external_id == "ext-1"
    assert stored.source_timestamp == "2026-01-01T07:00:00+00:00"
    assert stored.ingested_at == NOW.isoformat()
    assert stored.transformation_version == "body/1"
    assert stored.confidence == 0.8


def test_a_new_source_for_the_same_content_is_a_new_revision(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    with sqlite3.connect(db) as conn:
        a = ledger.record_revision(
            conn, user_id=user, record_type=RecordType.BODY_METRIC, record_key="k",
            provenance=_imported("health_connect_file"), content={"w": 1},
        )
        b = ledger.record_revision(
            conn, user_id=user, record_type=RecordType.BODY_METRIC, record_key="k",
            provenance=_imported("google_health"), content={"w": 1},
        )
    assert (a.revision, b.revision) == (1, 2) and b.status == "recorded"


def test_ledger_is_tenant_scoped(tmp_path: Any) -> None:
    db = _db(tmp_path)
    alice, bob = _users(db)
    save_body_metrics({"weight_kg": 80.0}, "2026-01-01", db, user_id=alice, provenance=_imported())
    save_body_metrics({"weight_kg": 90.0}, "2026-01-01", db, user_id=bob, provenance=_imported("health_connect_file"))
    correct_body_metrics(alice, "2026-01-01", {"weight_kg": 81.0}, db_path=db)

    assert len(get_record_lineage(alice, RecordType.BODY_METRIC, "2026-01-01", db)) == 2
    bob_lineage = get_record_lineage(bob, RecordType.BODY_METRIC, "2026-01-01", db)
    assert len(bob_lineage) == 1 and bob_lineage[0]["source"] == "health_connect_file"
    assert get_record_lineage("nobody", RecordType.BODY_METRIC, "2026-01-01", db) == []
    assert get_record_provenance("nobody", RecordType.BODY_METRIC, "2026-01-01", db) is None
    assert get_provenance_summary("nobody", RecordType.BODY_METRIC, db)["total"] == 0
    bob_view = get_record_provenance(bob, RecordType.BODY_METRIC, "2026-01-01", db)
    assert bob_view is not None and bob_view["kind"] == "imported"
    # Bob's reading and provenance are unaffected by Alice's correction.
    assert get_body_metrics(db_path=db, user_id=bob)[0]["weight_kg"] == 90.0


@pytest.mark.parametrize("user_id", ["", "   ", None])
def test_ledger_requires_an_owner(tmp_path: Any, user_id: Any) -> None:
    db = _db(tmp_path)
    with sqlite3.connect(db) as conn:
        with pytest.raises(ValueError, match="user_id"):
            ledger.record_revision(
                conn, user_id=user_id, record_type=RecordType.BODY_METRIC, record_key="k",
                provenance=_imported(), content={},
            )
        with pytest.raises(ValueError, match="user_id"):
            ledger.lineage(conn, user_id, RecordType.BODY_METRIC, "k")


def test_record_families_reject_the_wrong_kind_of_provenance(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    with sqlite3.connect(db) as conn:
        with pytest.raises(ValueError, match="cannot have derived"):
            ledger.record_revision(
                conn, user_id=user, record_type=RecordType.BODY_METRIC, record_key="k",
                provenance=_ai(), content={},
            )
        with pytest.raises(ValueError, match="cannot have imported"):
            ledger.record_revision(
                conn, user_id=user, record_type=RecordType.DAILY_PLAN, record_key="k",
                provenance=_imported(), content={},
            )
        with pytest.raises(ValueError):
            ledger.record_revision(
                conn, user_id=user, record_type="not_a_family", record_key="k",
                provenance=_imported(), content={},
            )


def test_unsafe_or_long_keys_are_hashed_consistently(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    key = "2026-01-01_Deadlift (Barbell) / O'Brien's"
    save_reasoning_log(key, "Deadlift", "because", db, user_id=user, provenance=_ai("xai/1"))
    assert ledger.normalise_key(key).startswith("h:")
    assert ledger.normalise_key(key) == ledger.normalise_key(key)
    view = get_record_provenance(user, RecordType.REASONING, key, db)
    assert view is not None and view["transformation_version"] == "xai/1"
    stored = _rows(db, "SELECT record_key FROM record_provenance")[0][0]
    assert key not in stored  # the raw, user-influenced key is never stored
    with pytest.raises(ValueError):
        ledger.normalise_key("   ")


# ---------------------------------------------------------------------------
# Imported records
# ---------------------------------------------------------------------------


def test_workout_import_records_hevy_identity_and_ignores_reimports(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    payload = {
        "workouts": [
            {"id": "hevy-w-1", "title": "Legs", "start_time": "2026-01-01T09:00:00Z", "exercises": []}
        ]
    }
    save_workout(payload, db, when="2026-01-01", user_id=user)
    save_workout(payload, db, when="2026-01-01", user_id=user)  # a rebuild re-imports it

    view = get_record_provenance(user, RecordType.WORKOUT, "hevy-w-1", db)
    assert view is not None
    assert view["kind"] == "imported" and view["source"] == "hevy"
    assert view["external_id"] == "hevy-w-1"
    assert view["source_timestamp"] == "2026-01-01T09:00:00+00:00"
    assert view["transformation_version"] == "hevy-parser/1"
    assert view["revision"] == 1
    assert _rows(db, "SELECT COUNT(*) FROM workout_history") == [(2,)]
    assert len(get_record_lineage(user, RecordType.WORKOUT, "hevy-w-1", db)) == 1


def test_workout_without_a_provider_id_still_gets_a_stable_key(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    payload = {"exercises": [{"title": "Squat", "sets": []}]}
    save_workout(payload, db, when="2026-01-01", user_id=user)
    save_workout(payload, db, when="2026-01-01", user_id=user)
    summary = get_provenance_summary(user, RecordType.WORKOUT, db)
    assert summary["total"] == 1
    assert summary["sources"][0]["source"] == "hevy"
    assert get_provenance_summary(user, RecordType.WORKOUT, db)["by_kind"] == {"imported": 1}


def test_progress_import_is_attributed_to_the_hevy_workout(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    payload = {
        "workouts": [
            {
                "id": "hevy-w-2",
                "title": "Pull",
                "start_time": "2026-01-03T09:00:00Z",
                "exercises": [{"title": "Row", "sets": [{"weight_kg": 60, "reps": 8}]}],
            }
        ]
    }
    summary = parse_workout(payload)
    assert summary is not None and summary.workout_id == "hevy-w-2"
    save_progress(summary, db, user_id=user)
    save_progress(summary, db, user_id=user)

    view = get_record_provenance(user, RecordType.EXERCISE_PROGRESS, "hevy-w-2", db)
    assert view is not None
    assert (view["source"], view["external_id"], view["revision"]) == ("hevy", "hevy-w-2", 1)
    assert view["source_timestamp"] == "2026-01-03T09:00:00+00:00"


def test_progress_without_a_workout_id_is_keyed_by_date_and_content(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    summary = WorkoutSummary(
        title="Day", date="2026-01-03", exercises=[ExerciseSummary("Squat", 100.0, 5, 3)]
    )
    save_progress(summary, db, user_id=user)
    other = WorkoutSummary(
        title="Day", date="2026-01-03", exercises=[ExerciseSummary("Squat", 105.0, 5, 3)]
    )
    save_progress(other, db, user_id=user)
    assert get_provenance_summary(user, RecordType.EXERCISE_PROGRESS, db)["total"] == 2


def test_body_metric_import_defaults_to_an_explicitly_unattributed_source(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    save_body_metrics({"weight_kg": 80.0}, "2026-01-01", db, user_id=user)
    view = get_record_provenance(user, RecordType.BODY_METRIC, "2026-01-01", db)
    assert view is not None
    assert view["kind"] == "imported" and view["source"] == UNATTRIBUTED_SOURCE
    assert view["label"] == "Imported, source not recorded"


def test_same_day_replacement_keeps_the_replaced_value_in_the_lineage(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    save_body_metrics({"weight_kg": 82.0, "body_fat_pct": 15.0}, "2026-01-01", db, user_id=user, provenance=_imported())
    save_body_metrics({"weight_kg": 81.5, "body_fat_pct": 14.6}, "2026-01-01", db, user_id=user, provenance=_imported())

    assert get_body_metrics(db_path=db, user_id=user)[0]["weight_kg"] == 81.5
    revisions = get_record_lineage(user, RecordType.BODY_METRIC, "2026-01-01", db)
    assert [r["revision"] for r in revisions] == [2, 1]
    assert revisions[1]["content"]["values"] == {"body_fat_pct": 15.0, "weight_kg": 82.0}
    assert revisions[0]["content"]["values"]["weight_kg"] == 81.5
    assert revisions[0]["supersedes_revision"] == 1


def test_field_level_sources_are_retained_for_merged_readings(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    save_body_metrics(
        {"weight_kg": 80.0, "muscle_pct": 40.0}, "2026-01-01", db, user_id=user,
        provenance=_imported("google_health"),
        field_sources={"muscle_pct": "health_connect_file"},
    )
    [revision] = get_record_lineage(user, RecordType.BODY_METRIC, "2026-01-01", db)
    assert revision["content"]["field_sources"] == {
        "weight_kg": "google_health",
        "muscle_pct": "health_connect_file",
    }


# ---------------------------------------------------------------------------
# Corrections preserve lineage
# ---------------------------------------------------------------------------


def test_correction_preserves_the_imported_original_and_links_to_it(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    save_body_metrics(
        {"weight_kg": 91.0, "body_fat_pct": 15.0}, "2026-01-01", db, user_id=user,
        provenance=_imported(source_timestamp="2026-01-01T06:30:00Z"),
    )

    result = correct_body_metrics(
        user, "2026-01-01", {"weight_kg": 81.0}, reason="Scale was on carpet", db_path=db
    )

    assert result["reading"]["weight_kg"] == 81.0
    assert result["reading"]["body_fat_pct"] == 15.0  # untouched fields are kept
    assert result["revision"] == 2
    current = result["provenance"]
    assert current["kind"] == "user_entered" and current["source"] == MANUAL_SOURCE
    assert current["corrected"] is True and current["label"] == "Corrected by you"
    assert current["authoritative"] is True
    assert current["supersedes_revision"] == 1
    assert current["note"] == "Scale was on carpet"
    assert get_body_metrics(db_path=db, user_id=user)[0]["weight_kg"] == 81.0

    correction, original = get_record_lineage(user, RecordType.BODY_METRIC, "2026-01-01", db)
    assert correction["content"]["corrected_fields"] == ["weight_kg"]
    assert correction["content"]["field_sources"] == {
        "weight_kg": MANUAL_SOURCE,
        "body_fat_pct": "google_health",
    }
    # The original imported value, its source and its timestamps are still there.
    assert original["kind"] == "imported" and original["source"] == "google_health"
    assert original["content"]["values"]["weight_kg"] == 91.0
    assert original["source_timestamp"] == "2026-01-01T06:30:00+00:00"
    assert original["corrected"] is False


def test_successive_corrections_form_an_unbroken_chain(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    save_body_metrics({"weight_kg": 91.0}, "2026-01-01", db, user_id=user, provenance=_imported())
    correct_body_metrics(user, "2026-01-01", {"weight_kg": 81.0}, db_path=db)
    correct_body_metrics(user, "2026-01-01", {"weight_kg": 82.0}, db_path=db)
    chain = get_record_lineage(user, RecordType.BODY_METRIC, "2026-01-01", db)
    assert [c["revision"] for c in chain] == [3, 2, 1]
    assert [c["supersedes_revision"] for c in chain] == [2, 1, None]
    assert [c["content"]["values"]["weight_kg"] for c in chain] == [82.0, 81.0, 91.0]
    assert _rows(db, "SELECT COUNT(*) FROM body_metrics") == [(1,)]  # still one current row


def test_user_correction_is_authoritative_over_a_later_automated_import(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    save_body_metrics({"weight_kg": 91.0}, "2026-01-01", db, user_id=user, provenance=_imported())
    correct_body_metrics(user, "2026-01-01", {"weight_kg": 81.0}, db_path=db)

    # The next scheduled run re-imports the same bad reading for that date.
    save_body_metrics({"weight_kg": 91.0}, "2026-01-01", db, user_id=user, provenance=_imported())
    save_body_metrics({"weight_kg": 91.0}, "2026-01-01", db, user_id=user, provenance=_imported())

    assert get_body_metrics(db_path=db, user_id=user)[0]["weight_kg"] == 81.0
    current = get_record_provenance(user, RecordType.BODY_METRIC, "2026-01-01", db)
    assert current is not None and current["kind"] == "user_entered"
    chain = get_record_lineage(user, RecordType.BODY_METRIC, "2026-01-01", db)
    held = [c for c in chain if not c["applied"]]
    assert len(held) == 1  # repeated imports are not duplicated
    assert held[0]["kind"] == "imported" and "takes precedence" in held[0]["note"]
    assert held[0]["content"]["values"]["weight_kg"] == 91.0
    applied = [c["revision"] for c in chain if c["applied"]]
    assert applied == [2, 1]


def test_a_later_user_correction_supersedes_the_held_import_period(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    save_body_metrics({"weight_kg": 91.0}, "2026-01-01", db, user_id=user, provenance=_imported())
    correct_body_metrics(user, "2026-01-01", {"weight_kg": 81.0}, db_path=db)
    save_body_metrics({"weight_kg": 90.0}, "2026-01-01", db, user_id=user, provenance=_imported())
    result = correct_body_metrics(user, "2026-01-01", {"weight_kg": 82.0}, db_path=db)
    assert result["provenance"]["supersedes_revision"] == 2  # the previous *applied* revision
    assert get_body_metrics(db_path=db, user_id=user)[0]["weight_kg"] == 82.0


def test_correcting_a_pre_ledger_reading_captures_its_original_value(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    with sqlite3.connect(db) as conn:  # a row written before provenance tracking
        conn.execute(
            "INSERT INTO body_metrics (date, weight_kg, body_fat_pct, user_id) "
            "VALUES ('2026-01-01', 91.0, 15.0, ?)", (user,),
        )
    assert get_body_metrics(db_path=db, user_id=user, include_provenance=True)[0]["provenance"] is None

    correct_body_metrics(user, "2026-01-01", {"weight_kg": 81.0}, db_path=db)

    correction, baseline = get_record_lineage(user, RecordType.BODY_METRIC, "2026-01-01", db)
    assert baseline["source"] == UNATTRIBUTED_SOURCE
    assert baseline["transformation_version"] == "legacy/0"
    assert "before provenance tracking" in baseline["note"]
    assert baseline["content"]["values"] == {"body_fat_pct": 15.0, "weight_kg": 91.0}
    assert correction["supersedes_revision"] == 1


def test_replacing_a_pre_ledger_reading_with_an_import_also_keeps_the_original(
    tmp_path: Any,
) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO body_metrics (date, weight_kg, user_id) VALUES ('2026-01-01', 91.0, ?)",
            (user,),
        )
    save_body_metrics({"weight_kg": 80.0}, "2026-01-01", db, user_id=user, provenance=_imported())
    newest, baseline = get_record_lineage(user, RecordType.BODY_METRIC, "2026-01-01", db)
    assert baseline["content"]["values"] == {"weight_kg": 91.0}
    assert newest["content"]["values"] == {"weight_kg": 80.0}


def test_manual_entry_for_a_date_without_a_reading_is_not_labelled_a_correction(
    tmp_path: Any,
) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    result = correct_body_metrics(user, "2026-01-01", {"weight_kg": 80.0, "resting_hr": 58.4}, db_path=db)
    assert result["revision"] == 1
    assert result["provenance"]["corrected"] is False
    assert result["provenance"]["label"] == "Entered by you"
    assert result["reading"]["resting_hr"] == 58
    assert get_body_metrics(db_path=db, user_id=user)[0]["weight_kg"] == 80.0


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({}, "at least one"),
        ({"weight_kg": 5.0}, "between"),
        ({"weight_kg": 900}, "between"),
        ({"body_fat_pct": 0}, "between"),
        ({"resting_hr": 300}, "between"),
        ({"weight_kg": float("nan")}, "between"),
        ({"weight_kg": float("inf")}, "between"),
        ({"weight_kg": True}, "must be a number"),
        ({"weight_kg": "81"}, "must be a number"),
        ({"password": 1}, "not a correctable"),
        ({"user_id": "other"}, "not a correctable"),
    ],
)
def test_invalid_corrections_are_rejected_without_side_effects(
    tmp_path: Any, changes: dict[str, Any], message: str
) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    save_body_metrics({"weight_kg": 91.0}, "2026-01-01", db, user_id=user, provenance=_imported())
    with pytest.raises(ValueError, match=message):
        correct_body_metrics(user, "2026-01-01", changes, db_path=db)
    assert len(get_record_lineage(user, RecordType.BODY_METRIC, "2026-01-01", db)) == 1
    assert get_body_metrics(db_path=db, user_id=user)[0]["weight_kg"] == 91.0


@pytest.mark.parametrize("when", ["", "yesterday", "2030-13-40", "2999-01-01", None])
def test_corrections_need_a_real_non_future_date(tmp_path: Any, when: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    with pytest.raises(ValueError):
        correct_body_metrics(user, when, {"weight_kg": 80.0}, db_path=db)


def test_a_correction_that_changes_nothing_adds_no_history(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    save_body_metrics({"weight_kg": 80.0}, "2026-01-01", db, user_id=user, provenance=_imported())
    with pytest.raises(ValueError, match="does not change"):
        correct_body_metrics(user, "2026-01-01", {"weight_kg": 80.0}, db_path=db)
    assert len(get_record_lineage(user, RecordType.BODY_METRIC, "2026-01-01", db)) == 1


@pytest.mark.parametrize("user_id", ["", "  ", None])
def test_corrections_require_an_authenticated_owner(tmp_path: Any, user_id: Any) -> None:
    db = _db(tmp_path)
    with pytest.raises(ValueError, match="user_id"):
        correct_body_metrics(user_id, "2026-01-01", {"weight_kg": 80.0}, db_path=db)


def test_correction_reason_is_bounded_and_stripped_of_control_characters(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    correct_body_metrics(
        user, "2026-01-01", {"weight_kg": 80.0}, reason="bad\x00\nscale " + "x" * 400, db_path=db
    )
    [revision] = get_record_lineage(user, RecordType.BODY_METRIC, "2026-01-01", db)
    assert "\x00" not in revision["note"] and "\n" not in revision["note"]
    assert len(revision["note"]) == 280


def test_a_correction_never_touches_another_users_reading(tmp_path: Any) -> None:
    db = _db(tmp_path)
    alice, bob = _users(db)
    save_body_metrics({"weight_kg": 90.0}, "2026-01-01", db, user_id=bob, provenance=_imported())
    correct_body_metrics(alice, "2026-01-01", {"weight_kg": 70.0}, db_path=db)
    assert get_body_metrics(db_path=db, user_id=bob)[0]["weight_kg"] == 90.0
    assert get_body_metrics(db_path=db, user_id=alice)[0]["weight_kg"] == 70.0
    assert len(get_record_lineage(bob, RecordType.BODY_METRIC, "2026-01-01", db)) == 1


def test_unscoped_legacy_save_does_not_delete_another_users_reading(tmp_path: Any) -> None:
    """The legacy no-user path used to delete every tenant's row for the date."""
    db = _db(tmp_path)
    alice, _ = _users(db)
    save_body_metrics({"weight_kg": 90.0}, "2026-01-01", db, user_id=alice)
    save_body_metrics({"weight_kg": 70.0}, "2026-01-01", db)
    save_body_metrics({"weight_kg": 71.0}, "2026-01-01", db)
    assert get_body_metrics(db_path=db, user_id=alice)[0]["weight_kg"] == 90.0
    assert len(get_body_metrics(db_path=db)) == 2  # alice's row + one legacy row


# ---------------------------------------------------------------------------
# Reads: provenance on records
# ---------------------------------------------------------------------------


def test_body_metrics_reads_can_include_per_reading_provenance(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    save_body_metrics({"weight_kg": 90.0}, "2026-01-01", db, user_id=user, provenance=_imported())
    save_body_metrics({"weight_kg": 91.0}, "2026-01-02", db, user_id=user, provenance=_imported("health_connect_file"))
    correct_body_metrics(user, "2026-01-02", {"weight_kg": 81.0}, db_path=db)

    plain = get_body_metrics(db_path=db, user_id=user)
    assert all("provenance" not in reading for reading in plain)

    first, second = get_body_metrics(db_path=db, user_id=user, include_provenance=True)
    assert first["provenance"]["kind"] == "imported"
    assert second["provenance"]["kind"] == "user_entered" and second["provenance"]["corrected"]
    assert "content" not in second["provenance"]  # values live in the lineage endpoint


def test_provenance_retrieval_fails_closed_without_an_owner(tmp_path: Any) -> None:
    db = _db(tmp_path)
    with pytest.raises(ValueError, match="user_id"):
        get_body_metrics(db_path=db, include_provenance=True)


# ---------------------------------------------------------------------------
# Derived records
# ---------------------------------------------------------------------------


def test_daily_plan_replacement_keeps_revision_history_and_ai_attribution(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, other = _users(db)
    save_daily_log("2026-01-01", 1, "Legs", "high", "plan A", "life", db, user_id=user, provenance=_ai())
    save_daily_log("2026-01-01", 1, "Legs", "high", "plan B", "life", db, user_id=user, provenance=_ai())
    save_daily_log("2026-01-01", 1, "Legs", "high", "other plan", "life", db, user_id=other, provenance=_ai())

    logs = get_daily_logs(limit=5, db_path=db, user_id=user, include_provenance=True)
    assert len(logs) == 1 and logs[0]["plan"] == "plan B"
    view = logs[0]["provenance"]
    assert view["kind"] == "derived" and view["authoritative"] is False
    assert view["source"] == "ai:gemini/test" and view["label"] == "AI recommendation"
    assert view["revision"] == 2 and view["supersedes_revision"] == 1
    lineage = get_record_lineage(user, RecordType.DAILY_PLAN, "2026-01-01", db)
    assert [item["revision"] for item in lineage] == [2, 1]
    assert all("plan A" not in json.dumps(item) for item in lineage)  # metadata only
    assert len(get_record_lineage(other, RecordType.DAILY_PLAN, "2026-01-01", db)) == 1


def test_derived_writers_default_to_unattributed_derived_provenance(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    save_daily_log("2026-01-01", 1, "Legs", "high", "plan", "life", db, user_id=user)
    save_checkin(1, "2026-01-01", 5, 4, "msg", db, user_id=user)
    save_dashboard_insight('{"advice": "x"}', db, user_id=user)
    save_deep_correlation("report", db, user_id=user)
    save_reasoning_log("ctx", "Squat", "why", db, user_id=user)
    expectations = [
        (RecordType.DAILY_PLAN, "2026-01-01"),
        (RecordType.CHECK_IN, "1"),
        (RecordType.DASHBOARD_INSIGHT, "current"),
        (RecordType.DEEP_CORRELATION, "current"),
        (RecordType.REASONING, "ctx"),
    ]
    for family, key in expectations:
        view = get_record_provenance(user, family, key, db)
        assert view is not None, family
        assert view["kind"] == "derived" and view["source"] == UNATTRIBUTED_SOURCE
        assert view["authoritative"] is False


def test_dashboard_insight_and_correlation_regeneration_form_a_chain(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, other = _users(db)
    save_dashboard_insight('{"advice": "a"}', db, user_id=user, provenance=_ai("dashboard-insight/1"))
    save_dashboard_insight('{"advice": "b"}', db, user_id=user, provenance=_ai("dashboard-insight/2"))
    save_deep_correlation("one", db, user_id=user, provenance=_ai("deep-correlation/1"))
    view = get_record_provenance(user, RecordType.DASHBOARD_INSIGHT, "current", db)
    assert view is not None
    assert (view["revision"], view["transformation_version"]) == (2, "dashboard-insight/2")
    assert get_record_provenance(other, RecordType.DASHBOARD_INSIGHT, "current", db) is None
    assert get_record_provenance(user, RecordType.DEEP_CORRELATION, "current", db) is not None


def test_ai_confidence_is_stored_where_a_producer_supplies_it(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    prov = Provenance.derived("rules:readiness", "readiness/2", confidence=0.65, ingested_at=NOW)
    save_dashboard_insight('{"advice": "a"}', db, user_id=user, provenance=prov)
    view = get_record_provenance(user, RecordType.DASHBOARD_INSIGHT, "current", db)
    assert view is not None and view["confidence"] == 0.65
    assert view["label"] == "Calculated by rules"


def test_summary_counts_current_revisions_by_kind_and_source(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    save_body_metrics({"weight_kg": 90.0}, "2026-01-01", db, user_id=user, provenance=_imported())
    save_body_metrics({"weight_kg": 91.0}, "2026-01-01", db, user_id=user, provenance=_imported())  # replaces
    save_body_metrics({"weight_kg": 92.0}, "2026-01-02", db, user_id=user, provenance=_imported("health_connect_file"))
    correct_body_metrics(user, "2026-01-02", {"weight_kg": 82.0}, db_path=db)
    summary = get_provenance_summary(user, RecordType.BODY_METRIC, db)
    assert summary["total"] == 2
    assert summary["by_kind"] == {"imported": 1, "user_entered": 1}
    assert {(s["kind"], s["source"], s["records"]) for s in summary["sources"]} == {
        ("imported", "google_health", 1),
        ("user_entered", MANUAL_SOURCE, 1),
    }
    assert summary["latest_ingested_at"]


def test_held_revisions_are_excluded_from_current_state_and_summary(tmp_path: Any) -> None:
    db = _db(tmp_path)
    user, _ = _users(db)
    save_body_metrics({"weight_kg": 90.0}, "2026-01-01", db, user_id=user, provenance=_imported())
    correct_body_metrics(user, "2026-01-01", {"weight_kg": 80.0}, db_path=db)
    save_body_metrics({"weight_kg": 90.0}, "2026-01-01", db, user_id=user, provenance=_imported())
    assert get_provenance_summary(user, RecordType.BODY_METRIC, db)["by_kind"] == {"user_entered": 1}
    assert get_record_provenance(user, RecordType.BODY_METRIC, "2026-01-01", db)["applied"] is True  # type: ignore[index]
