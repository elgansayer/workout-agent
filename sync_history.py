"""Script to fetch all historical workouts from Hevy and store them locally.

This is useful if you have lost your local database or want to sync to a
new device without losing your PRs and training history.
"""

import logging
from config import Config
from database import init_db, save_progress, save_workout
from hevy_client import get_all_workouts
from hevy_parser import parse_workout

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def sync_all():
    config = Config.load()
    if not config.hevy_api_key:
        logger.error("HEVY_API_KEY is not set in .env")
        return

    init_db(config.database_path)
    logger.info(f"Fetching all workouts from Hevy using DB {config.database_path}...")
    
    workouts = get_all_workouts(config.hevy_api_key)
    if not workouts:
        logger.info("No workouts found or failed to fetch.")
        return
        
    logger.info(f"Found {len(workouts)} workouts. Rebuilding history...")
    
    import sqlite3
    with sqlite3.connect(config.database_path, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("DELETE FROM workout_history")
        conn.execute("DELETE FROM exercise_progress")
        conn.commit()
    
    # Process from oldest to newest so history is built sequentially
    workouts.reverse()
    
    saved_count = 0
    for w in workouts:
        # Wrap it so _first_workout parses it correctly
        payload = {"workouts": [w]}
        
        when = w.get("start_time") or w.get("end_time")
        if when:
            when = when[:10]
            
        save_workout(payload, config.database_path, when=when)
        
        # We don't have rep_targets for historical blocks, so pass empty targets
        summary = parse_workout(payload, {})
        if summary:
            save_progress(summary, config.database_path)
            saved_count += 1
            
    logger.info(f"Finished rebuilding history. Processed {saved_count} workouts.")

if __name__ == "__main__":
    sync_all()
