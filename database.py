"""SQLite persistence for the workout agent.

Stores the current position in the 6-day cycle and a history of the raw Hevy
payloads so progress can be reviewed over time.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import date
from typing import Any, Iterator, TYPE_CHECKING

from program import SPLIT_NAME, TOTAL_DAYS

if TYPE_CHECKING:
    from hevy_parser import WorkoutSummary

DEFAULT_DB_PATH = "workout_agent.db"


@contextlib.contextmanager
def _connect(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """Create tables and seed the default programme state if empty."""
    with _connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS workout_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                hevy_payload TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS programme_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                current_day INTEGER NOT NULL,
                split_name TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS exercise_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                exercise_name TEXT NOT NULL,
                top_weight_kg REAL,
                top_reps INTEGER,
                sets INTEGER NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS hevy_routines (
                routine_key TEXT PRIMARY KEY,
                routine_id TEXT NOT NULL,
                content_hash TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS hevy_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS check_ins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number INTEGER NOT NULL,
                date TEXT NOT NULL,
                workouts_done INTEGER NOT NULL,
                weeks INTEGER NOT NULL,
                message TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                day INTEGER,
                focus TEXT NOT NULL,
                carb_tier TEXT NOT NULL,
                plan TEXT NOT NULL,
                lifestyle TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS body_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                weight_kg REAL,
                body_fat_pct REAL,
                muscle_pct REAL,
                resting_hr INTEGER
            )
            """
        )
        cursor.execute(
            """
            INSERT OR IGNORE INTO programme_state (id, current_day, split_name)
            VALUES (1, 1, ?)
            """,
            (SPLIT_NAME,),
        )
        cursor.execute(
            """
            INSERT OR IGNORE INTO hevy_meta (key, value)
            VALUES ('programme_start_date', ?)
            """,
            (date.today().isoformat(),),
        )


def get_current_day(db_path: str = DEFAULT_DB_PATH) -> int:
    """Return the current day in the cycle (1-6)."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT current_day FROM programme_state WHERE id = 1"
        ).fetchone()
    return int(row[0]) if row else 1


def advance_day(db_path: str = DEFAULT_DB_PATH) -> int:
    """Move to the next day, wrapping from TOTAL_DAYS back to 1."""
    current = get_current_day(db_path)
    nxt = current + 1 if current < TOTAL_DAYS else 1
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE programme_state SET current_day = ? WHERE id = 1", (nxt,)
        )
    return nxt


def save_workout(payload: Any, db_path: str = DEFAULT_DB_PATH) -> None:
    """Persist a raw Hevy payload for historical reference."""
    if payload is None:
        return
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO workout_history (date, hevy_payload) VALUES (?, ?)",
            (date.today().isoformat(), json.dumps(payload)),
        )


def save_progress(
    summary: "WorkoutSummary | None", db_path: str = DEFAULT_DB_PATH
) -> None:
    """Persist the per-exercise top sets from a parsed workout summary."""
    if summary is None:
        return
    today = date.today().isoformat()
    with _connect(db_path) as conn:
        for exercise in summary.exercises:
            conn.execute(
                """
                INSERT INTO exercise_progress
                    (date, exercise_name, top_weight_kg, top_reps, sets)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    today,
                    exercise.name,
                    exercise.top_weight_kg,
                    exercise.top_reps,
                    exercise.sets,
                ),
            )


def get_recent_bests(db_path: str = DEFAULT_DB_PATH) -> dict[str, dict[str, Any]]:
    """Return the most recently logged top set for each exercise by name."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT exercise_name, top_weight_kg, top_reps, sets, date
            FROM exercise_progress
            WHERE id IN (
                SELECT MAX(id) FROM exercise_progress GROUP BY exercise_name
            )
            ORDER BY exercise_name
            """
        ).fetchall()

    bests: dict[str, dict[str, Any]] = {}
    for name, weight, reps, sets, when in rows:
        bests[name] = {
            "top_weight_kg": weight,
            "top_reps": reps,
            "sets": sets,
            "date": when,
        }
    return bests


def get_progress_history(
    limit_per_exercise: int = 12, db_path: str = DEFAULT_DB_PATH
) -> dict[str, list[dict[str, Any]]]:
    """Return recent logged top sets per exercise, oldest first within each."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT exercise_name, top_weight_kg, top_reps, sets, date
            FROM exercise_progress
            ORDER BY exercise_name, id ASC
            """
        ).fetchall()

    series: dict[str, list[dict[str, Any]]] = {}
    for name, weight, reps, sets, when in rows:
        series.setdefault(name, []).append(
            {
                "top_weight_kg": weight,
                "top_reps": reps,
                "sets": sets,
                "date": when,
            }
        )
    # Keep only the most recent entries per exercise.
    return {
        name: entries[-limit_per_exercise:] for name, entries in series.items()
    }


def get_session_volumes(db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """Return a per-session training-load proxy, oldest first.

    Only the top set of each exercise is stored, so this is an estimate of
    relative session load (sum of top_weight x top_reps x sets), useful for
    spotting volume trends rather than an exact tonnage figure.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT date,
                   SUM(COALESCE(top_weight_kg, 0) * COALESCE(top_reps, 0) * sets) AS volume,
                   COUNT(*) AS exercises
            FROM exercise_progress
            GROUP BY date
            ORDER BY date ASC
            """
        ).fetchall()
    return [
        {"date": when, "volume": float(volume or 0), "exercises": int(exercises)}
        for when, volume, exercises in rows
    ]


def get_exercise_volumes(db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """Return total logged training-load per exercise, biggest first.

    Like ``get_session_volumes`` this is a top-set proxy (weight x reps x sets),
    useful for breaking volume down by exercise or muscle group.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT exercise_name,
                   SUM(COALESCE(top_weight_kg, 0) * COALESCE(top_reps, 0) * sets) AS volume,
                   COUNT(*) AS sessions
            FROM exercise_progress
            GROUP BY exercise_name
            ORDER BY volume DESC
            """
        ).fetchall()
    return [
        {"exercise": name, "volume": float(volume or 0), "sessions": int(sessions)}
        for name, volume, sessions in rows
    ]


def get_personal_records(db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """Return the all-time best estimated 1RM for each exercise.

    Uses the Epley estimate (weight x (1 + reps / 30)) across every logged top
    set, so personal records surface even as the rep targets change by block.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT exercise_name, top_weight_kg, top_reps, date
            FROM exercise_progress
            WHERE top_weight_kg IS NOT NULL AND top_reps IS NOT NULL
            """
        ).fetchall()

    best: dict[str, dict[str, Any]] = {}
    for name, weight, reps, when in rows:
        e1rm = float(weight) * (1 + int(reps) / 30)
        current = best.get(name)
        if current is None or e1rm > current["e1rm"]:
            best[name] = {
                "exercise": name,
                "e1rm": e1rm,
                "weight_kg": float(weight),
                "reps": int(reps),
                "date": when,
            }
    return sorted(best.values(), key=lambda r: r["e1rm"], reverse=True)


def get_routine_record(
    routine_key: str, db_path: str = DEFAULT_DB_PATH
) -> tuple[str, str] | None:
    """Return (routine_id, content_hash) for a synced routine, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT routine_id, content_hash FROM hevy_routines WHERE routine_key = ?",
            (routine_key,),
        ).fetchone()
    return (row[0], row[1]) if row else None


def save_routine_record(
    routine_key: str,
    routine_id: str,
    content_hash: str,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Persist the Hevy routine id and content hash for a routine key."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO hevy_routines (routine_key, routine_id, content_hash)
            VALUES (?, ?, ?)
            ON CONFLICT(routine_key) DO UPDATE SET
                routine_id = excluded.routine_id,
                content_hash = excluded.content_hash
            """,
            (routine_key, routine_id, content_hash),
        )


def get_meta(key: str, db_path: str = DEFAULT_DB_PATH) -> str | None:
    """Return a stored metadata value, or None if absent."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM hevy_meta WHERE key = ?", (key,)
        ).fetchone()
    return row[0] if row else None


def set_meta(key: str, value: str, db_path: str = DEFAULT_DB_PATH) -> None:
    """Store a metadata value under the given key."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO hevy_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def delete_routine_record(
    routine_key: str, db_path: str = DEFAULT_DB_PATH
) -> None:
    """Remove a tracked routine record (used when a routine is renamed)."""
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM hevy_routines WHERE routine_key = ?", (routine_key,)
        )


def get_programme_start_date(db_path: str = DEFAULT_DB_PATH) -> date:
    """Return the programme start date, defaulting to today if unset."""
    value = get_meta("programme_start_date", db_path)
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return date.today()


def save_checkin(
    number: int,
    when: str,
    workouts_done: int,
    weeks: int,
    message: str,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Persist a completed programme check-in for later review."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO check_ins (number, date, workouts_done, weeks, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (number, when, workouts_done, weeks, message),
        )


def get_checkins(
    limit: int = 20, db_path: str = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    """Return recent check-ins, most recent first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT number, date, workouts_done, weeks, message
            FROM check_ins
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "number": number,
            "date": when,
            "workouts_done": workouts_done,
            "weeks": weeks,
            "message": message,
        }
        for number, when, workouts_done, weeks, message in rows
    ]


def save_daily_log(
    when: str,
    day: int | None,
    focus: str,
    carb_tier: str,
    plan: str,
    lifestyle: str,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Log the full plan and lifestyle guidance issued for a day.

    One row per date: a re-run on the same day replaces the earlier entry so the
    log always holds the latest guidance that was sent.
    """
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM daily_log WHERE date = ?", (when,))
        conn.execute(
            """
            INSERT INTO daily_log (date, day, focus, carb_tier, plan, lifestyle)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (when, day, focus, carb_tier, plan, lifestyle),
        )


def get_daily_logs(
    limit: int = 30, db_path: str = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    """Return recent daily logs, most recent first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT date, day, focus, carb_tier, plan, lifestyle
            FROM daily_log
            ORDER BY date DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "date": when,
            "day": day,
            "focus": focus,
            "carb_tier": carb_tier,
            "plan": plan,
            "lifestyle": lifestyle,
        }
        for when, day, focus, carb_tier, plan, lifestyle in rows
    ]


def save_body_metrics(
    metrics: dict[str, Any] | None,
    when: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """Persist a body-composition reading (weight, body fat, muscle, resting HR).

    Accepts the dict produced by `health_connect.body_metrics_from_recovery`, or
    None (a no-op). One row per date: a later reading on the same day replaces
    the earlier one, so the morning weigh-in is what gets stored.
    """
    if not metrics:
        return
    when = when or date.today().isoformat()
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM body_metrics WHERE date = ?", (when,))
        conn.execute(
            """
            INSERT INTO body_metrics
                (date, weight_kg, body_fat_pct, muscle_pct, resting_hr)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                when,
                metrics.get("weight_kg"),
                metrics.get("body_fat_pct"),
                metrics.get("muscle_pct"),
                metrics.get("resting_hr"),
            ),
        )


def get_body_metrics(
    limit: int = 60, db_path: str = DEFAULT_DB_PATH
) -> list[dict[str, Any]]:
    """Return recent body-composition readings, oldest first for charting."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT date, weight_kg, body_fat_pct, muscle_pct, resting_hr
            FROM body_metrics
            ORDER BY date ASC, id ASC
            """
        ).fetchall()
    readings = [
        {
            "date": when,
            "weight_kg": weight,
            "body_fat_pct": body_fat,
            "muscle_pct": muscle,
            "resting_hr": resting_hr,
        }
        for when, weight, body_fat, muscle, resting_hr in rows
    ]
    return readings[-limit:]

