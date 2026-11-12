"""Runtime cutover coverage for #976.

Every surface resolves through the tenant-scoped programme runtime.  These tests
exercise dates, durations, lifecycle states and the absence of static-programme
imports in production modules.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import database
from programme_runtime import (
    load_programme_runtime,
    resolve_programme_runtime,
)

_DEFINITION: dict[str, Any] = {
    "name": "Synthetic programme",
    "source": "hevy",
    "cycle_weeks": 12,
    "programme_spec": {
        "start_date": "2026-09-01",
        "duration_weeks": 12,
        "goal": "hypertrophy",
    },
    "blocks": [
        {"number": 1, "name": "Foundation", "start_week": 1, "end_week": 6},
        {"number": 2, "name": "Build", "start_week": 7, "end_week": 12},
    ],
    "days": [
        {
            "number": 1,
            "routine_id": "routine-a",
            "focus": "Chest and back",
            "exercises": [
                {
                    "name": "Incline press",
                    "template_id": "press",
                    "sets": 3,
                    "rep_range": "8-12",
                    "scheme": "3 × 8-12",
                    "prescriptions": [
                        {"block_number": 1, "sets": 3, "rep_range": "8-12"},
                        {"block_number": 2, "sets": 4, "rep_range": "6-10"},
                    ],
                }
            ],
        },
        {
            "number": 2,
            "routine_id": "routine-b",
            "focus": "Legs and trunk",
            "exercises": [],
        },
    ],
}


def _active(duration: int) -> dict[str, Any]:
    definition = {
        **_DEFINITION,
        "cycle_weeks": duration,
        "programme_spec": {
            **_DEFINITION["programme_spec"],
            "duration_weeks": duration,
        },
        "blocks": [
            {
                "number": 1,
                "name": "Build",
                "start_week": 1,
                "end_week": duration,
            }
        ],
    }
    return {
        "id": 1,
        "source": "hevy",
        "updated_at": "2026-09-01T12:00:00Z",
        "definition": definition,
    }


def _user(db_path: str) -> str:
    database.init_db(db_path)
    return database.get_or_create_user(
        "cutover@example.test", "Cutover athlete", db_path
    )["id"]


def test_durations_use_their_real_dates_and_week_numbers(tmp_path: Path) -> None:
    for duration in (4, 8, 12, 20, 52):
        runtime = resolve_programme_runtime(
            _active(duration),
            [],
            today=date(2026, 9, 1),
        )
        assert runtime.status == "active"
        assert runtime.cycle_weeks == duration
        assert runtime.week == 1
        assert runtime.end_date == date.fromordinal(
            date(2026, 9, 1).toordinal() + duration * 7 - 1
        )


def test_missing_programme_is_an_explicit_setup_state(tmp_path: Path) -> None:
    db_path = str(tmp_path / "setup.db")
    user_id = _user(db_path)
    runtime = load_programme_runtime(user_id, db_path, today=date(2026, 9, 1))
    assert runtime.status == "setup_required"
    assert runtime.setup_required
    assert runtime.next_day is None
    assert runtime.days == []


def test_paused_and_completed_programmes_do_not_prescribe_a_workout(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "lifecycle.db")
    user_id = _user(db_path)
    now = "2026-09-01T00:00:00+00:00"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO programmes
                (user_id, source, template_key, definition, active,
                 created_at, updated_at, name, status, duration_weeks, start_date)
            VALUES (?, 'hevy', 'synthetic', '{}', 0, ?, ?, 'Synthetic', ?, 8, ?)
            """,
            (user_id, now, now, "paused", "2026-09-01"),
        )

    runtime = load_programme_runtime(user_id, db_path, today=date(2026, 9, 10))
    assert runtime.status == "paused"
    assert runtime.next_day is None

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE programmes SET status = 'completed' WHERE user_id = ?",
            (user_id,),
        )
    runtime = load_programme_runtime(user_id, db_path, today=date(2026, 9, 10))
    assert runtime.status == "completed"
    assert runtime.next_day is None


def test_current_block_prescription_is_materialised(tmp_path: Path) -> None:
    active = _active(12)
    active["definition"]["blocks"] = [
        {"number": 1, "name": "Foundation", "start_week": 1, "end_week": 6},
        {"number": 2, "name": "Build", "start_week": 7, "end_week": 12},
    ]
    runtime = resolve_programme_runtime(
        active,
        [],
        today=date(2026, 10, 19),  # week 7, block 2
    )
    assert runtime.status == "active"
    assert runtime.block["name"] == "Build"
    assert runtime.next_day is not None
    exercises = runtime.next_day["exercises"]
    assert exercises[0]["scheme"] == "4 × 6-10"


def test_rolling_programme_does_not_skip_after_missed_days() -> None:
    active = _active(12)
    active["definition"]["programme_spec"]["cadence"] = "rolling"
    first = {
        "id": "w-1",
        "routine_id": "routine-a",
        "end_time": "2026-09-01T12:00:00Z",
    }

    after_first = resolve_programme_runtime(
        active, [first], today=date(2026, 9, 8)
    )
    assert after_first.cadence == "rolling"
    assert after_first.next_day is not None
    assert after_first.next_day["routine_id"] == "routine-b"

    second = {
        "id": "w-2",
        "routine_id": "routine-b",
        "end_time": "2026-09-08T12:00:00Z",
    }
    after_second = resolve_programme_runtime(
        active, [first, second], today=date(2026, 9, 20)
    )
    assert after_second.next_day is not None
    assert after_second.next_day["routine_id"] == "routine-a"


def test_calendar_programme_advances_with_elapsed_days() -> None:
    active = _active(12)
    active["definition"]["programme_spec"]["cadence"] = "calendar"
    runtime = resolve_programme_runtime(active, [], today=date(2026, 9, 10))
    assert runtime.cadence == "calendar"
    assert runtime.next_day is not None
    assert runtime.next_day["routine_id"] == "routine-b"


def test_production_modules_do_not_import_the_retired_static_programme() -> None:
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in root.glob("*.py"):
        if path.name in {"program.py", "database.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        if "from program import" in text or "\nimport program\n" in text:
            offenders.append(path.name)
    assert offenders == []


def test_repeated_workout_ingestion_is_idempotent(tmp_path: Path) -> None:
    db_path = str(tmp_path / "ingest.db")
    user_id = _user(db_path)
    payload = {"workouts": [{"id": "w-1", "title": "Synthetic session"}]}
    database.save_workout(payload, db_path, when="2026-09-10", user_id=user_id)
    database.save_workout(payload, db_path, when="2026-09-10", user_id=user_id)
    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM workout_history WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
    assert count == 1


def test_updated_workout_replaces_the_stored_event(tmp_path: Path) -> None:
    db_path = str(tmp_path / "update.db")
    user_id = _user(db_path)
    database.save_workout(
        {"workouts": [{"id": "w-1", "title": "Synthetic session"}]},
        db_path,
        when="2026-09-10",
        user_id=user_id,
    )
    database.save_workout(
        {"workouts": [{"id": "w-1", "title": "Updated session"}]},
        db_path,
        when="2026-09-10",
        user_id=user_id,
    )
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT hevy_payload FROM workout_history WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    assert len(rows) == 1
    assert "Updated session" in rows[0][0]
