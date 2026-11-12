"""Per-user time zone, locale, unit system and accessibility preferences.

One typed contract replaces the deployment-wide ``TZ`` variable, the hard-coded
``en-GB`` locale and the kilogram-only displays. Everything is tenant scoped:
every read and write is keyed by the authenticated user's id and nothing here
accepts an owner identifier from a client.

Storage reuses the ``users`` row that already carried ``timezone`` and ``units``;
this module adds the remaining preference columns additively and idempotently.

Changing the time zone follows one rule: stored source timestamps are never
rewritten. Only derived local-day groupings (``exercise_progress.date``) are
recomputed from the persisted UTC instant, inside the same transaction as the
preference change, so readers never observe a new zone with stale groupings.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any

from database import DEFAULT_DB_PATH, _connect
from localisation import (
    DEFAULT_LOCALE,
    DEFAULT_TIMEZONE,
    DEFAULT_UNIT_SYSTEM,
    LOCALE_PROFILES,
    SUPPORTED_LOCALES,
    UNIT_SYSTEMS,
    local_date,
    normalise_locale,
    normalise_timezone,
    normalise_unit_system,
    parse_instant,
    supported_timezones,
    weight_unit,
)

logger = logging.getLogger(__name__)

# Accessibility preferences can only *add* accommodations to what the operating
# system already asks for, so "system" (follow the OS) is the explicit default.
MOTION_CHOICES: tuple[str, ...] = ("system", "reduce")
CONTRAST_CHOICES: tuple[str, ...] = ("system", "more")
TEXT_SIZE_CHOICES: tuple[str, ...] = ("default", "large", "x-large")

DEFAULT_MOTION = "system"
DEFAULT_CONTRAST = "system"
DEFAULT_TEXT_SIZE = "default"

# API field -> ``users`` column. ``units`` predates this module and keeps its
# column name; the API calls it ``unit_system``.
_COLUMNS: dict[str, str] = {
    "timezone": "timezone",
    "locale": "locale",
    "unit_system": "units",
    "motion": "motion",
    "contrast": "contrast",
    "text_size": "text_size",
}
# Column DDL for the additive migration. Names and defaults are code constants,
# never user input, so rendering them into DDL is safe.
_ADDED_USER_COLUMNS: dict[str, str] = {
    "locale": f"TEXT NOT NULL DEFAULT '{DEFAULT_LOCALE}'",
    "motion": f"TEXT NOT NULL DEFAULT '{DEFAULT_MOTION}'",
    "contrast": f"TEXT NOT NULL DEFAULT '{DEFAULT_CONTRAST}'",
    "text_size": f"TEXT NOT NULL DEFAULT '{DEFAULT_TEXT_SIZE}'",
}
_SELECT_SETTINGS = (
    "SELECT timezone, locale, units, motion, contrast, text_size "
    "FROM users WHERE id = ?"
)


class SettingsValidationError(ValueError):
    """A submitted preference is unknown or unsupported."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


class UserNotFoundError(LookupError):
    """The authenticated user id has no account row."""


@dataclass(frozen=True, slots=True)
class UserSettings:
    """A user's explicit, validated regional and accessibility preferences."""

    timezone: str = DEFAULT_TIMEZONE
    locale: str = DEFAULT_LOCALE
    unit_system: str = DEFAULT_UNIT_SYSTEM
    motion: str = DEFAULT_MOTION
    contrast: str = DEFAULT_CONTRAST
    text_size: str = DEFAULT_TEXT_SIZE

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SettingsUpdate:
    """Outcome of :func:`update_user_settings`."""

    settings: UserSettings
    changed: tuple[str, ...]
    regrouped_days: int


def settings_options() -> dict[str, Any]:
    """Describe every accepted value so clients never hard-code the lists."""
    return {
        "defaults": UserSettings().as_dict(),
        "timezones": list(supported_timezones()),
        "locales": [
            {"value": tag, "label": label}
            for tag, (label, _spelling) in LOCALE_PROFILES.items()
        ],
        "unit_systems": [
            {"value": "metric", "label": "Metric (kg)"},
            {"value": "imperial", "label": "Imperial (lb)"},
        ],
        "motion": [
            {"value": "system", "label": "Follow my device setting"},
            {"value": "reduce", "label": "Always reduce motion"},
        ],
        "contrast": [
            {"value": "system", "label": "Follow my device setting"},
            {"value": "more", "label": "Always increase contrast"},
        ],
        "text_size": [
            {"value": "default", "label": "Default"},
            {"value": "large", "label": "Large"},
            {"value": "x-large", "label": "Extra large"},
        ],
    }


def _choice(field: str, value: object, choices: tuple[str, ...]) -> str:
    if isinstance(value, str) and value.strip().lower() in choices:
        return value.strip().lower()
    raise SettingsValidationError(
        field, f"Unsupported {field.replace('_', ' ')}. Choose one of: {', '.join(choices)}."
    )


def validate_settings_changes(changes: Mapping[str, Any]) -> dict[str, str]:
    """Validate a partial update and return canonical values.

    Raises :class:`SettingsValidationError` for an unknown field, a ``None``
    value, or a value outside the supported set. Nothing is persisted here.
    """
    if not changes:
        raise SettingsValidationError("settings", "No settings were supplied.")
    clean: dict[str, str] = {}
    for field, value in changes.items():
        if field not in _COLUMNS:
            raise SettingsValidationError(str(field), f"Unknown setting '{field}'.")
        if field == "timezone":
            zone = normalise_timezone(value)
            if zone is None:
                raise SettingsValidationError(
                    field,
                    "Unsupported time zone. Use an IANA name such as Europe/London.",
                )
            clean[field] = zone
        elif field == "locale":
            tag = normalise_locale(value)
            if tag is None:
                raise SettingsValidationError(
                    field,
                    "Unsupported locale. Supported locales: "
                    f"{', '.join(SUPPORTED_LOCALES)}.",
                )
            clean[field] = tag
        elif field == "unit_system":
            system = normalise_unit_system(value)
            if system is None:
                raise SettingsValidationError(
                    field,
                    f"Unsupported unit system. Choose one of: {', '.join(UNIT_SYSTEMS)}.",
                )
            clean[field] = system
        elif field == "motion":
            clean[field] = _choice(field, value, MOTION_CHOICES)
        elif field == "contrast":
            clean[field] = _choice(field, value, CONTRAST_CHOICES)
        else:
            clean[field] = _choice(field, value, TEXT_SIZE_CHOICES)
    return clean


def _from_row(row: tuple[Any, ...]) -> UserSettings:
    """Build settings from a stored row, repairing unsupported values.

    Accessibility values are health-adjacent, so a repaired field is logged by
    name only, never by value.
    """
    timezone_name, locale, units, motion, contrast, text_size = row
    resolved = {
        "timezone": normalise_timezone(timezone_name),
        "locale": normalise_locale(locale),
        "unit_system": normalise_unit_system(units),
        "motion": motion if motion in MOTION_CHOICES else None,
        "contrast": contrast if contrast in CONTRAST_CHOICES else None,
        "text_size": text_size if text_size in TEXT_SIZE_CHOICES else None,
    }
    defaults = UserSettings()
    values: dict[str, str] = {}
    for field, value in resolved.items():
        if value is None:
            logger.warning("Stored %s preference is unsupported; using default.", field)
            values[field] = getattr(defaults, field)
        else:
            values[field] = value
    return UserSettings(**values)


def ensure_settings_schema(
    cursor: sqlite3.Cursor, *, deployment_timezone: str | None = None
) -> None:
    """Additively add the preference columns. Safe to run on every start.

    * ``users`` gains locale/motion/contrast/text_size; ``timezone`` and
      ``units`` already exist.
    * ``exercise_progress.performed_at`` stores the immutable UTC instant a
      workout started, the source its derived ``date`` grouping is computed from.
    * The first time the columns appear on an existing database, accounts that
      still hold the untouched UTC default inherit the deployment ``TZ`` they
      were being scheduled in, so the upgrade does not silently move anyone's
      daily run. From then on only per-user settings apply.
    """
    user_columns = {row[1] for row in cursor.execute("PRAGMA table_info(users)")}
    added = False
    for name, definition in _ADDED_USER_COLUMNS.items():
        if name not in user_columns:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
            added = True
    if added:
        inherited = normalise_timezone(
            deployment_timezone
            if deployment_timezone is not None
            else os.environ.get("TZ", "")
        )
        if inherited and inherited != DEFAULT_TIMEZONE:
            cursor.execute(
                "UPDATE users SET timezone = ? WHERE timezone IS NULL OR timezone = ?",
                (inherited, DEFAULT_TIMEZONE),
            )

    progress_columns = {
        row[1] for row in cursor.execute("PRAGMA table_info(exercise_progress)")
    }
    if "performed_at" not in progress_columns:
        cursor.execute("ALTER TABLE exercise_progress ADD COLUMN performed_at TEXT")


def get_user_settings(user_id: str, db_path: str = DEFAULT_DB_PATH) -> UserSettings:
    """Return the user's settings, or the explicit defaults for an unknown id."""
    with _connect(db_path) as conn:
        row = conn.execute(_SELECT_SETTINGS, (user_id,)).fetchone()
    return _from_row(row) if row else UserSettings()


def get_user_settings_or_defaults(
    user_id: str, db_path: str = DEFAULT_DB_PATH
) -> UserSettings:
    """Like :func:`get_user_settings`, but never fails a background job.

    Scheduled jobs run unattended, so an unreadable settings row degrades to the
    explicit defaults (logged) instead of losing the user's whole daily run.
    """
    try:
        return get_user_settings(user_id, db_path)
    except sqlite3.Error:
        logger.warning("Could not read user settings; using explicit defaults.")
        return UserSettings()


def recompute_local_days(
    conn: sqlite3.Connection, user_id: str, timezone_name: str
) -> int:
    """Re-derive one user's workout-day groupings in *timezone_name*.

    Only ``exercise_progress.date`` is written, and only for rows that carry a
    persisted source instant. ``performed_at`` and every metric are untouched,
    other users' rows are never matched, and rows without an instant (entered
    as a bare calendar day) keep their day because nothing can recompute them.
    Returns the number of rows whose grouping changed.
    """
    rows = conn.execute(
        "SELECT id, performed_at, date FROM exercise_progress "
        "WHERE user_id = ? AND performed_at IS NOT NULL",
        (user_id,),
    ).fetchall()
    updates: list[tuple[str, int, str]] = []
    for row_id, performed_at, current_day in rows:
        if parse_instant(performed_at) is None:
            continue
        derived = local_date(performed_at, timezone_name).isoformat()
        if derived != current_day:
            updates.append((derived, row_id, user_id))
    conn.executemany(
        "UPDATE exercise_progress SET date = ? WHERE id = ? AND user_id = ?",
        updates,
    )
    return len(updates)


def update_user_settings(
    user_id: str,
    changes: Mapping[str, Any],
    db_path: str = DEFAULT_DB_PATH,
) -> SettingsUpdate:
    """Validate and persist a partial settings update for *user_id*.

    Omitted fields keep their stored value. A time-zone change regroups that
    user's workout days in the same transaction. Raises
    :class:`SettingsValidationError` or :class:`UserNotFoundError`.
    """
    validated = validate_settings_changes(changes)
    with _connect(db_path) as conn:
        row = conn.execute(_SELECT_SETTINGS, (user_id,)).fetchone()
        if row is None:
            raise UserNotFoundError(user_id)
        current = _from_row(row)
        merged = replace(current, **validated)
        changed = tuple(f for f in validated if getattr(current, f) != validated[f])
        stored = (
            merged.timezone,
            merged.locale,
            merged.unit_system,
            merged.motion,
            merged.contrast,
            merged.text_size,
        )
        if tuple(row) == stored:
            return SettingsUpdate(current, (), 0)
        conn.execute(
            "UPDATE users SET timezone = ?, locale = ?, units = ?, motion = ?, "
            "contrast = ?, text_size = ? WHERE id = ?",
            (*stored, user_id),
        )
        regrouped = (
            recompute_local_days(conn, user_id, merged.timezone)
            if "timezone" in changed
            else 0
        )
    return SettingsUpdate(merged, changed, regrouped)


def export_preferences(settings: UserSettings) -> dict[str, Any]:
    """Describe how to read an export for this user.

    Exports carry source data unchanged (UTC instants, kilograms) plus the
    owner's preferences, so a consumer can render local days and the owner's
    unit system without guessing or re-deriving anything.
    """
    return {
        **settings.as_dict(),
        "weight_unit": weight_unit(settings.unit_system),
        "timestamps": "ISO-8601 UTC; local days are derived using timezone",
        "stored_weight_unit": "kg",
    }
