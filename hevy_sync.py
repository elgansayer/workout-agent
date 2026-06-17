"""Builds and syncs the training programme as ready-to-use Hevy routines.

Each unique session in the split is pushed to a dedicated "Workout Agent" folder
in Hevy so that, every day, the right routine is already there to start. When the
programme in program.py changes, re-running the sync updates the routines in
place (matched by a content hash, so unchanged routines are left untouched).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from config import Config
from database import (
    delete_routine_record,
    get_meta,
    get_programme_start_date,
    get_routine_record,
    save_routine_record,
    set_meta,
)
from hevy_client import (
    create_routine,
    create_routine_folder,
    get_exercise_history,
    get_routine_folders,
    get_routines,
    update_routine,
)
from program import (
    Block,
    Exercise,
    block_for_week,
    day_exercises,
    day_focus,
    week_in_cycle,
)

logger = logging.getLogger(__name__)

FOLDER_NAME = "Workout Agent"
FOLDER_META_KEY = "routine_folder_id"
DEFAULT_REST_SECONDS = 90
# Smallest plate jump to apply once the top of the rep range is reached.
WEIGHT_INCREMENT_KG = 2.5

# The three distinct sessions in the 6-day split (days 1-3 repeat as 4-6).
_SESSION_DAYS = (1, 2, 3)

# Routines whose titles changed; the existing Hevy routine is renamed in place.
_TITLE_MIGRATIONS = {"Chest & Back": "Back, Deadlifts & Chest"}


def _parse_rep_range(rep_range: str) -> tuple[int | None, int | None]:
    """Return (start, end) reps; end is None when it is a single target."""
    numbers = [int(n) for n in re.findall(r"\d+", rep_range)]
    if not numbers:
        return None, None
    if len(numbers) == 1:
        return numbers[0], None
    return numbers[0], numbers[-1]


def _build_set(rep_range: str, weight_kg: float | None = None) -> dict[str, Any]:
    start, end = _parse_rep_range(rep_range)
    if start is not None and end is not None:
        return {
            "type": "normal",
            "weight_kg": weight_kg,
            "reps": None,
            "rep_range": {"start": start, "end": end},
        }
    return {"type": "normal", "weight_kg": weight_kg, "reps": start}


def _build_exercise(
    exercise: Exercise, weight_kg: float | None = None
) -> dict[str, Any] | None:
    template_id = exercise.template_id
    if template_id is None:
        logger.warning(
            "No Hevy template mapped for '%s'; skipping it in the routine.",
            exercise.name,
        )
        return None
    return {
        "exercise_template_id": template_id,
        "superset_id": None,
        "rest_seconds": DEFAULT_REST_SECONDS,
        "notes": exercise.note or None,
        "sets": [
            _build_set(exercise.rep_range, weight_kg) for _ in range(exercise.sets)
        ],
    }


def _build_exercises(
    exercises: list[Exercise], weights: dict[str, float] | None = None
) -> list[dict[str, Any]]:
    weights = weights or {}
    built = [_build_exercise(ex, weights.get(ex.name)) for ex in exercises]
    return [ex for ex in built if ex is not None]


def _latest_top_set(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the heaviest set from the most recently logged session."""
    if not history:
        return None
    latest_time = max((e.get("workout_start_time") or "") for e in history)
    recent = [e for e in history if (e.get("workout_start_time") or "") == latest_time]
    return max(
        recent,
        key=lambda e: ((e.get("weight_kg") or 0.0), (e.get("reps") or 0)),
    )


def _target_weight(exercise: Exercise, history: list[dict[str, Any]]) -> float | None:
    """Double progression: bump weight once the top of the rep range was hit."""
    best = _latest_top_set(history)
    if best is None:
        return None
    weight = best.get("weight_kg")
    if weight is None:
        return None
    reps = best.get("reps")
    start, end = _parse_rep_range(exercise.rep_range)
    top = end if end is not None else start
    if reps is not None and top is not None and reps >= top:
        return round(weight + WEIGHT_INCREMENT_KG, 2)
    return float(weight)


def _compute_target_weights(config: Config, block: Block) -> dict[str, float]:
    """Fetch Hevy history per exercise and compute today's target weights."""
    weights: dict[str, float] = {}
    seen: set[str] = set()
    for day in _SESSION_DAYS:
        for exercise in day_exercises(day, block):
            if exercise.name in seen:
                continue
            seen.add(exercise.name)
            template_id = exercise.template_id
            if template_id is None:
                continue
            history = get_exercise_history(config.hevy_api_key, template_id)
            if not history:
                continue
            target = _target_weight(exercise, history)
            if target is not None:
                weights[exercise.name] = target
    return weights



def _content_hash(title: str, exercises: list[dict[str, Any]], notes: str) -> str:
    blob = json.dumps(
        {"title": title, "notes": notes, "exercises": exercises},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _routine_id_from_response(result: dict[str, Any] | None) -> str | None:
    """Extract a routine id from a create/update response of any known shape.

    Hevy returns the routine wrapped under a "routine" key, and that value may
    be a single object or a single-element list.
    """
    if not isinstance(result, dict):
        return None
    routine = result.get("routine", result)
    if isinstance(routine, list):
        routine = routine[0] if routine else None
    if isinstance(routine, dict) and "id" in routine:
        return str(routine["id"])
    return None



def _ensure_folder(config: Config) -> int | None:
    """Return the Hevy folder id for our routines, creating it if needed."""
    stored = get_meta(FOLDER_META_KEY, config.database_path)
    if stored is not None:
        return int(stored)

    folders = get_routine_folders(config.hevy_api_key)
    if folders is not None:
        for folder in folders:
            if folder.get("title") == FOLDER_NAME:
                folder_id = int(folder["id"])
                set_meta(FOLDER_META_KEY, str(folder_id), config.database_path)
                return folder_id

    created = create_routine_folder(config.hevy_api_key, FOLDER_NAME)
    if created is None:
        logger.warning("Could not create the '%s' folder in Hevy.", FOLDER_NAME)
        return None
    folder = created.get("routine_folder", created)
    folder_id = int(folder["id"])
    set_meta(FOLDER_META_KEY, str(folder_id), config.database_path)
    return folder_id


def _find_existing_routine_id(api_key: str, title: str) -> str | None:
    """Look up a routine id by title (recovers if the local DB was lost)."""
    routines = get_routines(api_key)
    if not routines:
        return None
    for routine in routines:
        if routine.get("title") == title:
            return str(routine["id"])
    return None


def _migrate_titles(config: Config) -> None:
    """Carry tracked routine records across renamed titles so the existing
    Hevy routine is updated in place rather than orphaned."""
    for old_title, new_title in _TITLE_MIGRATIONS.items():
        old_record = get_routine_record(old_title, config.database_path)
        if old_record is None:
            continue
        if get_routine_record(new_title, config.database_path) is None:
            routine_id, _ = old_record
            # Empty hash forces a PUT on next sync, renaming the routine.
            save_routine_record(new_title, routine_id, "", config.database_path)
        delete_routine_record(old_title, config.database_path)


def _sync_session(config: Config, title: str, exercises: list[Exercise],
                  folder_id: int | None, notes: str,
                  weights: dict[str, float] | None = None) -> str:
    """Create or update a single routine. Returns a short status string."""
    built = _build_exercises(exercises, weights)
    content_hash = _content_hash(title, built, notes)

    record = get_routine_record(title, config.database_path)
    if record is None:
        # Maybe it exists in Hevy already but is not tracked locally.
        existing_id = _find_existing_routine_id(config.hevy_api_key, title)
        if existing_id is not None:
            record = (existing_id, "")

    if record is not None:
        routine_id, previous_hash = record
        if previous_hash == content_hash:
            return f"{title}: up to date"
        payload = {"routine": {"title": title, "notes": notes, "exercises": built}}
        result = update_routine(config.hevy_api_key, routine_id, payload)
        if result is None:
            return f"{title}: update failed"
        save_routine_record(title, routine_id, content_hash, config.database_path)
        return f"{title}: updated"

    payload = {
        "routine": {
            "title": title,
            "folder_id": folder_id,
            "notes": notes,
            "exercises": built,
        }
    }
    result = create_routine(config.hevy_api_key, payload)
    if result is None:
        return f"{title}: create failed"
    routine_id = _routine_id_from_response(result)
    if routine_id is None:
        return f"{title}: created (id missing)"
    save_routine_record(title, routine_id, content_hash, config.database_path)
    return f"{title}: created"


def sync_routines(config: Config) -> list[str]:
    """Sync all distinct sessions to Hevy. Returns per-routine status lines."""
    _migrate_titles(config)
    start = get_programme_start_date(config.database_path)
    week = week_in_cycle(start)
    block = block_for_week(week)
    notes = (
        f"Week {week} of 12, Block {block.number}: {block.name} "
        f"(weeks {block.weeks}). {block.focus} "
        "Built and kept in sync by your workout agent."
    )
    folder_id = _ensure_folder(config)
    weights = (
        _compute_target_weights(config, block) if config.hevy_prefill_weights else {}
    )
    statuses: list[str] = []
    for day in _SESSION_DAYS:
        statuses.append(
            _sync_session(
                config,
                day_focus(day),
                day_exercises(day, block),
                folder_id,
                notes,
                weights,
            )
        )
    return statuses
