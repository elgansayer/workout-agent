"""SQLite persistence for the workout agent.

Stores the current position in the 6-day cycle and a history of the raw Hevy
payloads so progress can be reviewed over time.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import date
from typing import Any, Iterator

from program import SPLIT_NAME, TOTAL_DAYS

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
            INSERT OR IGNORE INTO programme_state (id, current_day, split_name)
            VALUES (1, 1, ?)
            """,
            (SPLIT_NAME,),
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
