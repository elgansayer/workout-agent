"""Domain, migration and tenant-isolation tests for programme revisions."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from typing import Any

import database
import pytest
from programme_domain import (
    AdaptationDecisionInput,
    ExercisePrescription,
    ProgrammeBlock,
    ProgrammeDraft,
    ProgrammeNotFoundError,
    ProgrammeRoutine,
    ProgrammeService,
    ProgrammeValidationError,
    ProgrammeWeekState,
    RoutineWorkoutMatch,
)


def _users(db: str) -> tuple[str, str]:
    first = database.get_or_create_user("athlete-a@example.test", "A", db)
    second = database.get_or_create_user("athlete-b@example.test", "B", db)
    return first["id"], second["id"]


def _routine(position: int, source_id: str = "routine-a") -> ProgrammeRoutine:
    return ProgrammeRoutine(
        source_routine_id=source_id,
        title=f"Routine {source_id}",
        snapshot_hash=f"hash-{source_id}",
        rotation_position=position,
        folder_id="folder-1",
        provider_updated_at="2026-08-18T12:00:00Z",
        snapshot={
            "id": source_id,
            "title": f"Routine {source_id}",
            "exercises": [{"template_id": "bench", "sets": 3}],
        },
    )


def _draft(
    *,
    user_id: str | None = None,
    duration: int = 8,
) -> ProgrammeDraft:
    routines = [
        _routine(1, "routine-b"),
        _routine(2, "routine-a"),
    ]
    if user_id is not None:
        routines = [
            ProgrammeRoutine(
                source_routine_id=routine.source_routine_id,
                title=routine.title,
                snapshot_hash=routine.snapshot_hash,
                rotation_position=routine.rotation_position,
                owner_user_id=user_id,
            )
            for routine in routines
        ]
    return ProgrammeDraft(
        name="Synthetic hypertrophy block",
        goal="hypertrophy",
        duration_weeks=duration,
        start_date=date(2026, 9, 1),
        settings={"schema_version": "programme-v2"},
        routines=routines,
        blocks=[
            ProgrammeBlock(
                position=1,
                key="foundation",
                name="Foundation",
                start_week=1,
                end_week=4,
                objective="Establish a recoverable baseline.",
                targets={"weekly_sets": {"chest": 10.0}},
                adaptation_policy={"max_set_delta": 1},
                exit_criteria={"adherence": 0.8},
            ),
            ProgrammeBlock(
                position=2,
                key="overload",
                name="Overload",
                start_week=5,
                end_week=8,
                objective="Progress volume while maintaining technique.",
                targets={"weekly_sets": {"chest": 12.0}},
            ),
        ],
        prescriptions=[
            ExercisePrescription(
                exercise_template_id="bench",
                exercise_title="Bench Press",
                role="primary_compound",
                routine_position=1,
                block_position=1,
                set_target={"min": 3, "target": 3, "max": 4},
                rep_target={"min": 6, "max": 8},
                effort_target={"rir_min": 2, "rir_max": 3},
                load_anchor={"type": "last_successful", "value": 80.0, "unit": "kg"},
                rest_seconds=150,
                progression_method="double_progression",
                progression_rules={"max_load_percent": 5},
                confidence=0.82,
            ),
            ExercisePrescription(
                exercise_template_id="row",
                exercise_title="Chest Supported Row",
                role="accessory",
                routine_position=2,
                block_position=2,
                week_number=5,
                set_target={"min": 2, "target": 3, "max": 3},
                rep_target={"min": 8, "max": 12},
                rest_seconds=90,
                confidence=0.7,
            ),
        ],
    )


def _service(tmp_path: Any) -> tuple[ProgrammeService, str, str, str]:
    db = str(tmp_path / "domain.db")
    database.init_db(db)
    first, second = _users(db)
    return ProgrammeService(db), db, first, second


def test_fresh_database_creates_domain_tables_and_indexes(tmp_path: Any) -> None:
    db = str(tmp_path / "fresh.db")
    database.init_db(db)

    with sqlite3.connect(db) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "programmes",
            "programme_revisions",
            "programme_routines",
            "programme_blocks",
            "exercise_prescriptions",
            "programme_week_states",
            "adaptation_decisions",
            "routine_workout_matches",
        } <= tables
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert "idx_programmes_one_active_per_user" in indexes
        for column, referenced in (
            ("user_id", "users"),
        ):
            for table in (
                "programme_revisions",
                "programme_routines",
                "programme_blocks",
                "exercise_prescriptions",
                "programme_week_states",
                "adaptation_decisions",
                "routine_workout_matches",
            ):
                foreign_keys = conn.execute(
                    'SELECT "table", "from" FROM pragma_foreign_key_list(?)',
                    (table,),
                ).fetchall()
                assert (referenced, column) in foreign_keys, table


def test_draft_round_trip_preserves_order_blocks_and_prescriptions(
    tmp_path: Any,
) -> None:
    service, _db, first, _second = _service(tmp_path)
    draft = _draft(user_id=first)

    persisted = service.persist_draft(first, draft)
    revision = persisted.current_revision
    assert persisted.status == "draft"
    assert revision is not None
    assert [routine.rotation_position for routine in revision.routines] == [1, 2]
    assert [routine.source_routine_id for routine in revision.routines] == [
        "routine-b",
        "routine-a",
    ]
    assert [(block.position, block.start_week, block.end_week) for block in revision.blocks] == [
        (1, 1, 4),
        (2, 5, 8),
    ]
    assert revision.prescriptions[0].set_target == {"min": 3, "target": 3, "max": 4}
    assert revision.prescriptions[0].load_anchor["unit"] == "kg"
    assert revision.settings == {"schema_version": "programme-v2"}

    active = service.activate(first, persisted.id)
    assert active.status == "active"
    assert active.activated_at
    assert service.get_active_programme(first).id == persisted.id
    assert service.get_active_revision(first) is not None
    assert service.get_active_revision(first).revision_number == 1

    week = service.get_programme_week(first, persisted.id, 5)
    assert week is not None
    assert week.start_date == date(2026, 9, 29)
    assert [block.key for block in week.blocks] == ["overload"]
    assert len(week.prescriptions) == 2
    assert week.state is None


def test_repository_methods_cannot_cross_tenant_boundaries(tmp_path: Any) -> None:
    service, _db, first, second = _service(tmp_path)
    programme = service.persist_draft(first, _draft(user_id=first))
    service.activate(first, programme.id)
    revision = programme.current_revision
    assert revision is not None

    assert service.get_programme(second, programme.id) is None
    assert service.get_active_programme(second) is None
    assert service.list_revision_history(second, programme.id) == []
    assert service.get_revision(second, programme.id, 1) is None
    assert service.get_programme_week(second, programme.id, 1) is None
    assert service.list_adaptation_decisions(second, programme.id) == []
    assert service.list_routine_workout_matches(second, programme.id) == []

    with pytest.raises(ProgrammeNotFoundError):
        service.pause(second, programme.id)
    with pytest.raises(ProgrammeNotFoundError):
        service.save_week_state(
            second,
            programme.id,
            revision.id,
            ProgrammeWeekState(week_number=1),
        )
    with pytest.raises(ProgrammeNotFoundError):
        service.save_routine_workout_match(
            second,
            RoutineWorkoutMatch(
                programme_id=programme.id,
                revision_id=revision.id,
                routine_position=1,
                source_routine_id="routine-b",
                workout_id="workout-1",
                score=0.9,
                confidence=0.8,
                method="provider_id",
            ),
        )


def test_activation_keeps_exactly_one_active_programme(tmp_path: Any) -> None:
    service, _db, first, _second = _service(tmp_path)
    first_programme = service.persist_draft(first, _draft(user_id=first))
    second_draft = _draft(user_id=first)
    second_programme = service.persist_draft(first, second_draft)

    service.activate(first, first_programme.id)
    service.activate(first, second_programme.id)

    first_reloaded = service.get_programme(first, first_programme.id)
    assert first_reloaded is not None
    assert first_reloaded.status == "archived"
    assert service.get_active_programme(first).id == second_programme.id

    with sqlite3.connect(service.repository.db_path) as conn:
        active_count = conn.execute(
            "SELECT COUNT(*) FROM programmes WHERE user_id = ? AND status = 'active'",
            (first,),
        ).fetchone()[0]
    assert active_count == 1


def test_concurrent_activation_attempts_leave_one_active(tmp_path: Any) -> None:
    service, db, first, _second = _service(tmp_path)
    programmes = [
        service.persist_draft(first, _draft(user_id=first)).id for _ in range(3)
    ]

    def activate(programme_id: int) -> str:
        return ProgrammeService(db).activate(first, programme_id).status

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(activate, programmes))

    assert results == ["active", "active", "active"]
    with sqlite3.connect(db) as conn:
        active_rows = conn.execute(
            "SELECT COUNT(*) FROM programmes WHERE user_id = ? AND status = 'active'",
            (first,),
        ).fetchone()[0]
    assert active_rows == 1


def test_revisions_are_immutable_and_adaptations_append(tmp_path: Any) -> None:
    service, _db, first, _second = _service(tmp_path)
    programme = service.persist_draft(first, _draft(user_id=first))
    original = programme.current_revision
    assert original is not None
    service.activate(first, programme.id)

    adapted = service.create_revision_from_adaptation(
        first,
        programme.id,
        _draft(user_id=first, duration=8),
        AdaptationDecisionInput(
            scope={"exercise_template_id": "bench", "week": 5},
            inputs={"completed_sets": 9, "target_rir": 2},
            output={"set_target": {"min": 3, "target": 4, "max": 4}},
            confidence=0.76,
            explanation="Target sets met at the upper rep range.",
            confirmed=True,
        ),
    )
    assert adapted.revision_number == 2
    history = service.list_revision_history(first, programme.id)
    assert [revision.revision_number for revision in history] == [1, 2]
    decisions = service.list_adaptation_decisions(first, programme.id)
    assert len(decisions) == 1
    assert decisions[0].from_revision_id == original.id
    assert decisions[0].to_revision_id == adapted.id
    assert decisions[0].confirmed is True
    assert service.get_revision(first, programme.id, 1) == original

    with sqlite3.connect(service.repository.db_path) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "UPDATE programme_revisions SET reason = 'tampered' WHERE id = ?",
                (original.id,),
            )
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "DELETE FROM programme_revisions WHERE id = ?", (original.id,)
            )


def test_rebase_creates_new_revision_without_mutating_previous(tmp_path: Any) -> None:
    service, _db, first, _second = _service(tmp_path)
    programme = service.persist_draft(first, _draft(user_id=first))
    first_revision = programme.current_revision
    assert first_revision is not None

    rebase_draft = _draft(user_id=first, duration=10)
    rebase_draft = replace(
        rebase_draft,
        blocks=[
            ProgrammeBlock(
                position=1, key="foundation", name="Foundation", start_week=1, end_week=5
            ),
            ProgrammeBlock(
                position=2, key="overload", name="Overload", start_week=6, end_week=10
            ),
        ],
    )
    rebased = service.create_revision_from_rebase(
        first, programme.id, rebase_draft, reason="rebase"
    )
    assert rebased.revision_number == 2
    assert rebased.reason == "rebase"
    assert rebased.blocks[-1].end_week == 10
    unchanged = service.get_revision(first, programme.id, 1)
    assert unchanged is not None
    assert unchanged.blocks[-1].end_week == 8


def test_foreign_owned_routine_snapshot_is_rejected(tmp_path: Any) -> None:
    service, _db, first, second = _service(tmp_path)
    draft = _draft(user_id=second)

    with pytest.raises(ProgrammeValidationError, match="owned by another user"):
        service.persist_draft(first, draft)


def test_block_order_bounds_and_duration_are_enforced(tmp_path: Any) -> None:
    service, _db, first, _second = _service(tmp_path)
    overlapping = _draft(user_id=first)
    overlapping = ProgrammeDraft(
        **{
            **overlapping.__dict__,
            "blocks": [
                ProgrammeBlock(
                    position=1, key="a", name="A", start_week=1, end_week=5
                ),
                ProgrammeBlock(
                    position=2, key="b", name="B", start_week=5, end_week=8
                ),
            ],
        }
    )
    with pytest.raises(ProgrammeValidationError, match="non-overlapping"):
        service.preview_draft(first, overlapping)

    out_of_bounds = _draft(user_id=first, duration=6)
    with pytest.raises(ProgrammeValidationError, match="outside the programme"):
        service.preview_draft(first, out_of_bounds)

    reversed_block = _draft(user_id=first)
    reversed_block = ProgrammeDraft(
        **{
            **reversed_block.__dict__,
            "blocks": [
                ProgrammeBlock(
                    position=1, key="a", name="A", start_week=5, end_week=4
                )
            ],
        }
    )
    with pytest.raises(ProgrammeValidationError, match="starts after"):
        service.preview_draft(first, reversed_block)


def test_routine_positions_and_prescription_scope_are_explicit(tmp_path: Any) -> None:
    service, _db, first, _second = _service(tmp_path)
    bad_order = _draft(user_id=first)
    bad_order = ProgrammeDraft(
        **{
            **bad_order.__dict__,
            "routines": [_routine(1, "a"), _routine(1, "b")],
        }
    )
    with pytest.raises(ProgrammeValidationError, match="rotation positions"):
        service.preview_draft(first, bad_order)

    bad_prescription = _draft(user_id=first)
    bad_prescription = ProgrammeDraft(
        **{
            **bad_prescription.__dict__,
            "prescriptions": [
                ExercisePrescription(
                    exercise_template_id="bench",
                    exercise_title="Bench Press",
                    role="primary_compound",
                    routine_position=99,
                )
            ],
        }
    )
    with pytest.raises(ProgrammeValidationError, match="unselected routine"):
        service.preview_draft(first, bad_prescription)


def test_week_state_and_routine_match_round_trip(tmp_path: Any) -> None:
    service, _db, first, _second = _service(tmp_path)
    programme = service.persist_draft(first, _draft(user_id=first))
    revision = programme.current_revision
    assert revision is not None

    state = service.save_week_state(
        first,
        programme.id,
        revision.id,
        ProgrammeWeekState(
            week_number=5,
            planned={"sessions": 3},
            actual={"sessions": 2},
            adherence=0.67,
            fatigue=4.0,
            review_state="in_review",
        ),
    )
    assert state.adherence == 0.67
    week = service.get_programme_week(first, programme.id, 5)
    assert week is not None
    assert week.state is not None
    assert week.state.review_state == "in_review"

    match = service.save_routine_workout_match(
        first,
        RoutineWorkoutMatch(
            programme_id=programme.id,
            revision_id=revision.id,
            routine_position=1,
            source_routine_id="routine-b",
            workout_id="workout-42",
            score=0.94,
            confidence=0.91,
            method="provider_id",
            user_confirmed=True,
        ),
    )
    assert match.user_confirmed is True
    assert service.list_routine_workout_matches(first, programme.id)[0].workout_id == (
        "workout-42"
    )


def test_hevy_source_deletion_does_not_cascade_delete_revisions(
    tmp_path: Any,
) -> None:
    service, db, first, _second = _service(tmp_path)
    programme = service.persist_draft(first, _draft(user_id=first))
    service.activate(first, programme.id)

    with sqlite3.connect(db) as conn:
        foreign_keys = conn.execute(
            'SELECT "table" FROM pragma_foreign_key_list(?)',
            ("programme_routines",),
        ).fetchall()
        assert ("hevy_routines",) not in foreign_keys
        conn.execute(
            "INSERT OR IGNORE INTO hevy_routines "
            "(user_id, routine_key, routine_id, content_hash) "
            "VALUES (?, 'routine-a', 'routine-a', 'hash-routine-a')",
            (first,),
        )
        conn.execute("DELETE FROM hevy_routines WHERE user_id = ?", (first,))

    reloaded = service.get_programme(first, programme.id)
    assert reloaded is not None
    assert reloaded.current_revision is not None


def test_legacy_static_programme_is_archived_and_preserved(tmp_path: Any) -> None:
    db = str(tmp_path / "legacy.db")
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                display_name TEXT,
                created_at TEXT NOT NULL,
                timezone TEXT DEFAULT 'UTC',
                units TEXT DEFAULT 'metric'
            );
            CREATE TABLE programmes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id),
                source TEXT NOT NULL,
                template_key TEXT,
                definition TEXT,
                active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, template_key)
            );
            INSERT INTO users
                (id, email, display_name, created_at)
            VALUES ('legacy-a', 'legacy-a@example.test', 'Legacy', '2026-01-01');
            INSERT INTO programmes
                (user_id, source, template_key, definition, active,
                 created_at, updated_at)
            VALUES
                ('legacy-a', 'template', 'hybrid_powerbuilding',
                 '{"cycle_weeks": 12, "days": []}', 1,
                 '2026-01-01', '2026-01-01');
            """
        )

    database.init_db(db)
    database.init_db(db)

    with sqlite3.connect(db) as conn:
        programme = conn.execute(
            "SELECT status, active, source, current_revision_id "
            "FROM programmes WHERE id = 1"
        ).fetchone()
        assert programme[0] == "archived"
        assert programme[1] == 0
        assert programme[2] == "legacy_static"
        assert programme[3] is not None
        revisions = conn.execute(
            "SELECT revision_number, reason, engine_version, settings_json "
            "FROM programme_revisions WHERE programme_id = 1"
        ).fetchall()
        assert len(revisions) == 1
        assert revisions[0][0] == 1
        assert revisions[0][1] == "legacy_static"
        assert revisions[0][2] == "legacy-static"
        assert json.loads(revisions[0][3])["cycle_weeks"] == 12

    service = ProgrammeService(db)
    assert service.get_active_programme("legacy-a") is None
    new_user = database.get_or_create_user("new@example.test", "New", db)
    assert service.get_active_programme(new_user["id"]) is None
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM programmes WHERE user_id = ?",
                (new_user["id"],),
            ).fetchone()[0]
            == 0
        )


def test_partial_domain_schema_is_completed_idempotently(tmp_path: Any) -> None:
    db = str(tmp_path / "partial.db")
    database.init_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE adaptation_decisions")
        conn.execute("DROP TABLE routine_workout_matches")
        conn.execute("DROP INDEX IF EXISTS idx_programmes_user_status")

    database.init_db(db)
    database.init_db(db)

    with sqlite3.connect(db) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"adaptation_decisions", "routine_workout_matches"} <= tables
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert "idx_programmes_user_status" in indexes


def test_legacy_active_hevy_programme_stays_active_for_compatibility(
    tmp_path: Any,
) -> None:
    db = str(tmp_path / "compat.db")
    database.init_db(db)
    user = database.get_or_create_user("compat@example.test", "Compat", db)
    database.set_active_programme(
        user["id"],
        "hevy",
        "compat-programme",
        {"preview_token": "token", "programme_spec": {"duration_weeks": 8}},
        db,
    )

    database.init_db(db)
    service = ProgrammeService(db)
    active = service.get_active_programme(user["id"])
    assert active is not None
    assert active.status == "active"
    assert active.current_revision is not None
    assert active.current_revision.reason == "legacy_migration"


def test_legacy_reactivation_cannot_break_database_initialisation(
    tmp_path: Any,
) -> None:
    db = str(tmp_path / "reactivate.db")
    database.init_db(db)
    user = database.get_or_create_user("reactivate@example.test", "Re", db)
    database.set_active_programme(
        user["id"],
        "hevy",
        "hevy:first",
        {"preview_token": "first", "programme_spec": {"duration_weeks": 8}},
        db,
    )
    database.init_db(db)
    database.set_active_programme(
        user["id"],
        "hevy",
        "hevy:second",
        {"preview_token": "second", "programme_spec": {"duration_weeks": 8}},
        db,
    )

    database.init_db(db)

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT template_key, active, status FROM programmes "
            "WHERE user_id = ? ORDER BY id ASC",
            (user["id"],),
        ).fetchall()
    assert [row[0] for row in rows] == ["hevy:first", "hevy:second"]
    assert sum(row[1] for row in rows) == 1
    assert [row[2] for row in rows] == ["archived", "active"]


def test_old_active_flag_cannot_override_new_archive(tmp_path: Any) -> None:
    service, db, first, _second = _service(tmp_path)
    programme = service.persist_draft(first, _draft(user_id=first))
    service.activate(first, programme.id)
    service.archive(first, programme.id)

    database.init_db(db)
    assert service.get_active_programme(first) is None

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT status, active, archived_at FROM programmes WHERE id = ?",
            (programme.id,),
        ).fetchone()
    assert row[0] == "archived"
    assert row[1] == 0
    assert row[2]
