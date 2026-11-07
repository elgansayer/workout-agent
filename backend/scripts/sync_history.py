from __future__ import annotations

import sqlite3

"""Script to fetch all historical workouts from Hevy and store them locally.

This is useful if you have lost your local database or want to sync to a
new device without losing your PRs and training history.

Run from the repo root:
    python scripts/sync_history.py
"""

import argparse
import logging
import sys
from pathlib import Path

# Allow the script to find sibling modules when run from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import Config
from database import init_db, require_tenant, save_progress, save_workout
from hevy_client import get_all_workouts
from hevy_parser import parse_workout
from legacy_ownership import is_deployment_owner
from tenancy import TenantRequiredError
from tenant_isolation import OwnershipError, authorize_owner

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _rebuild_tables(db_path: str, user_id: str | None = None) -> None:
    """Clear one user's workout_history and exercise_progress for repopulation.

    The delete is always tenant scoped: it never clears other users' history.
    """
    uid = require_tenant(user_id, db_path)
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("DELETE FROM workout_history WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM exercise_progress WHERE user_id = ?", (uid,))
        conn.commit()


def sync_all(
    api_key: str,
    db_path: str | None = None,
    *,
    user_id: str | None = None,
) -> dict[str, int | str]:
    """Fetch all Hevy workouts and rebuild local workout_history + exercise_progress.

    Args:
        api_key: Hevy API key.
        db_path: Optional database path (falls back to Config default).
        user_id: The user whose history is rebuilt. Required outside the explicit
            non-production local mode; history is never rebuilt for everyone.

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
    try:
        user_id = require_tenant(user_id, db_path)
        user_id = authorize_owner(db_path, user_id).user_id
    except (TenantRequiredError, OwnershipError) as exc:
        return {"error": str(exc)}
    logger.info("Fetching all workouts from Hevy (DB %s)...", db_path)

    workouts = get_all_workouts(api_key)
    if not workouts:
        logger.info("No workouts found or failed to fetch.")
        return {"workouts_found": 0, "processed": 0}

    logger.info("Found %d workouts. Rebuilding history...", len(workouts))

    _rebuild_tables(db_path, user_id=user_id)

    # Process from oldest to newest so history is built sequentially.
    workouts.reverse()

    saved_count = 0
    for w in workouts:
        # Wrap it so _first_workout parses it correctly.
        payload = {"workouts": [w]}

        when = w.get("start_time") or w.get("end_time")
        if when:
            when = when[:10]

        save_workout(payload, db_path, when=when, user_id=user_id)

        # We don't have rep_targets for historical blocks, so pass empty targets.
        summary = parse_workout(payload, {})
        if summary:
            save_progress(summary, db_path, user_id=user_id)
            saved_count += 1

    logger.info("Finished rebuilding history. Processed %d workouts.", saved_count)
    return {"workouts_found": len(workouts), "processed": saved_count}


def main(argv: list[str] | None = None) -> int:
    """Rebuild history with the deployment-wide ``HEVY_API_KEY``.

    That key belongs to the deployment owner only, so it is refused for any other
    account; use ``main.py --sync-history --user-id ...`` for those, which uses
    the account's own stored key.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--user-id",
        help="Account whose history is rebuilt (required in production).",
    )
    args = parser.parse_args(argv)

    config = Config.load()
    if not config.hevy_api_key:
        logger.error("HEVY_API_KEY is not set in .env")
        return 1
    init_db(config.database_path)
    try:
        user_id = require_tenant(args.user_id, config.database_path)
    except TenantRequiredError as exc:
        logger.error("%s", exc)
        return 1
    if not is_deployment_owner(user_id, config.database_path):
        logger.error(
            "HEVY_API_KEY belongs to the deployment owner; run "
            "'main.py --sync-history --user-id ...' to use this account's own key."
        )
        return 1
    result = sync_all(config.hevy_api_key, config.database_path, user_id=user_id)
    if "error" in result:
        logger.error("%s", result["error"])
        return 1
    logger.info("%s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
