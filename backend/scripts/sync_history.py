from __future__ import annotations

import sqlite3

"""Script to fetch all historical workouts from Hevy and store them locally.

This is useful if you have lost your local database or want to sync to a
new device without losing your PRs and training history.

Run from the repo root:
    python scripts/sync_history.py
"""

import logging
import sys
from pathlib import Path

# Allow the script to find sibling modules when run from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import Config
from database import init_db, save_progress, save_workout
from hevy_client import get_all_workouts
from hevy_parser import parse_workout
from ownership import AuthenticatedOwner, owner_for_legacy_tenant, owner_id

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _rebuild_tables(db_path: str, *, owner: AuthenticatedOwner) -> None:
    """Clear the owner's workout_history and exercise_progress rows."""
    user_id = owner_id(owner)
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("DELETE FROM workout_history WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM exercise_progress WHERE user_id = ?", (user_id,))
        conn.commit()


def sync_all(
    api_key: str,
    db_path: str | None = None,
    *,
    owner: AuthenticatedOwner,
) -> dict[str, int | str]:
    """Fetch all Hevy workouts and rebuild local workout_history + exercise_progress.

    Args:
        api_key: Hevy API key.
        db_path: Optional database path (falls back to Config default).
        owner: The authenticated owner the rebuilt rows belong to.

    Returns:
        A summary dict with keys ``workouts_found`` (int) and
        ``processed`` (int), or ``error`` (str) on failure.
    """
    if not api_key:
        return {"error": "No Hevy API key configured."}

    if db_path is None:
        config = Config.load()
        db_path = config.database_path

    init_db(db_path)
    logger.info("Fetching all workouts from Hevy (DB %s)...", db_path)

    workouts = get_all_workouts(api_key)
    if not workouts:
        logger.info("No workouts found or failed to fetch.")
        return {"workouts_found": 0, "processed": 0}

    logger.info("Found %d workouts. Rebuilding history...", len(workouts))

    _rebuild_tables(db_path, owner=owner)

    # Process from oldest to newest so history is built sequentially.
    workouts.reverse()

    saved_count = 0
    for w in workouts:
        # Wrap it so _first_workout parses it correctly.
        payload = {"workouts": [w]}

        when = w.get("start_time") or w.get("end_time")
        if when:
            when = when[:10]

        save_workout(payload, db_path, when=when, owner=owner)

        # We don't have rep_targets for historical blocks, so pass empty targets.
        summary = parse_workout(payload, {})
        if summary:
            save_progress(summary, db_path, owner=owner)
            saved_count += 1

    logger.info("Finished rebuilding history. Processed %d workouts.", saved_count)
    return {"workouts_found": len(workouts), "processed": saved_count}


if __name__ == "__main__":
    config = Config.load()
    if not config.hevy_api_key:
        logger.error("HEVY_API_KEY is not set in .env")
    else:
        init_db(config.database_path)
        owner = owner_for_legacy_tenant(config.database_path)
        result = sync_all(config.hevy_api_key, config.database_path, owner=owner)
        if "error" in result:
            logger.error("%s", result["error"])
        else:
            logger.info("%s", result)
