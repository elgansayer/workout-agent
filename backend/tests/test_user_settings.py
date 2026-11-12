"""Per-user settings: persistence, validation, tenant isolation and day regrouping."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from database import (
    _connect,
    get_or_create_user,
    get_session_volumes,
    init_db,
    save_progress,
    save_workout,
)
from hevy_parser import ExerciseSummary, WorkoutSummary
from user_settings import (
    SettingsValidationError,
    UserNotFoundError,
    UserSettings,
    ensure_settings_schema,
    export_preferences,
    get_user_settings,
    get_user_settings_or_defaults,
    recompute_local_days,
    settings_options,
    update_user_settings,
    validate_settings_changes,
)


@pytest.fixture()
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "settings.db")
    init_db(path)
    return path


def _user(db: str, email: str) -> str:
    return get_or_create_user(email, "Synthetic", db)["id"]


def _workout(started_at: str, name: str = "Squat", weight: float = 100.0) -> WorkoutSummary:
    return WorkoutSummary(
        title="Synthetic session",
        date=started_at,
        exercises=[ExerciseSummary(name, weight, 5, 3)],
    )


def _progress_rows(db: str, user_id: str) -> list[tuple[str, str | None, float | None]]:
    with _connect(db) as conn:
        return conn.execute(
            "SELECT date, performed_at, top_weight_kg FROM exercise_progress "
            "WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()


# ---------------------------------------------------------------------------
# Explicit defaults
# ---------------------------------------------------------------------------


def test_defaults_are_explicit_and_documented() -> None:
    assert UserSettings().as_dict() == {
        "timezone": "UTC",
        "locale": "en-GB",
        "unit_system": "metric",
        "motion": "system",
        "contrast": "system",
        "text_size": "default",
    }


def test_new_accounts_start_on_the_explicit_defaults(db: str) -> None:
    assert get_user_settings(_user(db, "new@example.test"), db) == UserSettings()


def test_unknown_account_ids_get_defaults_not_someone_elses_settings(db: str) -> None:
    update_user_settings(_user(db, "a@example.test"), {"unit_system": "imperial"}, db)
    assert get_user_settings("not-a-user", db) == UserSettings()


def test_options_publish_every_accepted_value(db: str) -> None:
    options = settings_options()
    assert options["defaults"] == UserSettings().as_dict()
    assert {"value": "en-GB", "label": "English (United Kingdom)"} in options["locales"]
    assert "Europe/London" in options["timezones"]
    assert [o["value"] for o in options["unit_systems"]] == ["metric", "imperial"]
    assert [o["value"] for o in options["motion"]] == ["system", "reduce"]
    assert [o["value"] for o in options["contrast"]] == ["system", "more"]
    assert [o["value"] for o in options["text_size"]] == ["default", "large", "x-large"]


# ---------------------------------------------------------------------------
# Persistence and tenant isolation
# ---------------------------------------------------------------------------


def test_settings_persist_per_user_and_never_leak_between_users(db: str) -> None:
    alice = _user(db, "alice@example.test")
    bob = _user(db, "bob@example.test")

    result = update_user_settings(
        alice,
        {
            "timezone": "Pacific/Auckland",
            "locale": "en-NZ",
            "unit_system": "imperial",
            "motion": "reduce",
            "contrast": "more",
            "text_size": "x-large",
        },
        db,
    )

    assert set(result.changed) == {
        "timezone",
        "locale",
        "unit_system",
        "motion",
        "contrast",
        "text_size",
    }
    assert get_user_settings(alice, db) == result.settings
    assert get_user_settings(alice, db).timezone == "Pacific/Auckland"
    assert get_user_settings(bob, db) == UserSettings()


def test_partial_updates_keep_every_other_field(db: str) -> None:
    uid = _user(db, "partial@example.test")
    update_user_settings(uid, {"timezone": "Europe/London", "unit_system": "imperial"}, db)

    update_user_settings(uid, {"motion": "reduce"}, db)

    settings = get_user_settings(uid, db)
    assert (settings.timezone, settings.unit_system, settings.motion) == (
        "Europe/London",
        "imperial",
        "reduce",
    )


def test_resubmitting_current_values_is_a_no_op(db: str) -> None:
    uid = _user(db, "noop@example.test")
    update_user_settings(uid, {"timezone": "Europe/London"}, db)

    result = update_user_settings(uid, {"timezone": "Europe/London"}, db)

    assert result.changed == () and result.regrouped_days == 0


def test_values_are_canonicalised_before_they_are_stored(db: str) -> None:
    uid = _user(db, "canon@example.test")
    update_user_settings(uid, {"locale": "en_us", "unit_system": " IMPERIAL "}, db)
    settings = get_user_settings(uid, db)
    assert (settings.locale, settings.unit_system) == ("en-US", "imperial")


def test_unit_system_reuses_the_existing_users_units_column(db: str) -> None:
    uid = _user(db, "column@example.test")
    update_user_settings(uid, {"unit_system": "imperial"}, db)
    with _connect(db) as conn:
        assert conn.execute("SELECT units FROM users WHERE id = ?", (uid,)).fetchone() == (
            "imperial",
        )


def test_updating_an_unknown_account_fails_without_creating_it(db: str) -> None:
    with pytest.raises(UserNotFoundError):
        update_user_settings("ghost", {"unit_system": "imperial"}, db)
    with _connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE id = 'ghost'").fetchone() == (0,)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timezone", "Mars/Base"),
        ("timezone", "../../etc/passwd"),
        ("timezone", ""),
        ("timezone", None),
        ("locale", "fr-FR"),
        ("locale", "de-DE"),
        ("locale", "xx"),
        ("locale", "en"),
        ("locale", None),
        ("unit_system", "stone"),
        ("unit_system", 1),
        ("motion", "warp"),
        ("contrast", "extreme"),
        ("text_size", "huge"),
        ("text_size", None),
    ],
)
def test_unsupported_values_are_rejected_and_nothing_is_stored(
    db: str, field: str, value: object
) -> None:
    uid = _user(db, "invalid@example.test")
    with pytest.raises(SettingsValidationError) as caught:
        update_user_settings(uid, {field: value}, db)
    assert caught.value.field == field
    assert get_user_settings(uid, db) == UserSettings()


def test_unsupported_locale_message_lists_what_is_supported() -> None:
    with pytest.raises(SettingsValidationError, match="en-GB.*en-US"):
        validate_settings_changes({"locale": "fr-FR"})


def test_unknown_fields_and_owner_claims_are_rejected() -> None:
    for field in ("favourite_colour", "user_id", "owner_id"):
        with pytest.raises(SettingsValidationError, match="Unknown setting"):
            validate_settings_changes({field: "x"})


def test_an_empty_update_is_rejected() -> None:
    with pytest.raises(SettingsValidationError):
        validate_settings_changes({})


def test_one_bad_field_rejects_the_whole_update(db: str) -> None:
    uid = _user(db, "atomic@example.test")
    with pytest.raises(SettingsValidationError):
        update_user_settings(uid, {"timezone": "Europe/London", "locale": "fr-FR"}, db)
    assert get_user_settings(uid, db) == UserSettings()


# ---------------------------------------------------------------------------
# Stored values that are no longer supported
# ---------------------------------------------------------------------------


def _corrupt(db: str, user_id: str) -> None:
    with _connect(db) as conn:
        conn.execute(
            "UPDATE users SET timezone = 'Mars/Base', locale = 'fr-FR', units = 'stone', "
            "motion = 'warp', contrast = 'extreme', text_size = 'huge' WHERE id = ?",
            (user_id,),
        )


def test_unsupported_stored_values_read_back_as_the_explicit_defaults(db: str) -> None:
    uid = _user(db, "stale@example.test")
    _corrupt(db, uid)
    assert get_user_settings(uid, db) == UserSettings()


def test_a_supported_value_still_wins_next_to_unsupported_ones(db: str) -> None:
    uid = _user(db, "mixed@example.test")
    _corrupt(db, uid)
    with _connect(db) as conn:
        conn.execute("UPDATE users SET locale = 'en-US' WHERE id = ?", (uid,))
    settings = get_user_settings(uid, db)
    assert settings.locale == "en-US" and settings.timezone == "UTC"


def test_saving_repairs_stale_unsupported_values(db: str) -> None:
    uid = _user(db, "repair@example.test")
    _corrupt(db, uid)

    # The visible locale already equals the default, yet the stored junk is replaced.
    update_user_settings(uid, {"locale": "en-GB"}, db)

    with _connect(db) as conn:
        row = conn.execute(
            "SELECT timezone, locale, units, motion, contrast, text_size FROM users "
            "WHERE id = ?",
            (uid,),
        ).fetchone()
    assert row == ("UTC", "en-GB", "metric", "system", "system", "default")


def test_stale_values_are_logged_by_field_name_only(
    db: str, caplog: pytest.LogCaptureFixture
) -> None:
    uid = _user(db, "log@example.test")
    _corrupt(db, uid)
    with caplog.at_level("WARNING", logger="user_settings"):
        get_user_settings(uid, db)
    text = caplog.text
    assert "motion" in text and "locale" in text
    for raw in ("warp", "fr-FR", "extreme", "Mars/Base"):
        assert raw not in text


def test_background_jobs_degrade_to_defaults_when_settings_are_unreadable(
    tmp_path: Path,
) -> None:
    empty = str(tmp_path / "empty.db")  # no schema at all
    assert get_user_settings_or_defaults("anyone", empty) == UserSettings()
    with pytest.raises(sqlite3.OperationalError):
        get_user_settings("anyone", empty)


# ---------------------------------------------------------------------------
# Time-zone changes: source timestamps stay, derived local days are regrouped
# ---------------------------------------------------------------------------


def test_workout_day_is_derived_in_the_owners_zone_and_the_instant_is_kept(db: str) -> None:
    uid = _user(db, "tz@example.test")
    update_user_settings(uid, {"timezone": "Pacific/Auckland"}, db)

    save_progress(_workout("2026-03-02T20:30:00+00:00"), db, user_id=uid)

    assert _progress_rows(db, uid) == [("2026-03-03", "2026-03-02T20:30:00+00:00", 100.0)]


def test_changing_time_zone_regroups_days_but_never_touches_source_data(db: str) -> None:
    uid = _user(db, "regroup@example.test")
    payload = {"workouts": [{"title": "Synthetic", "start_time": "2026-03-02T20:30:00+00:00"}]}
    save_workout(payload, db, when="2026-03-02", user_id=uid)
    save_progress(_workout("2026-03-02T20:30:00+00:00", "Squat", 100.0), db, user_id=uid)
    save_progress(_workout("2026-03-04T08:00:00+00:00", "Bench", 80.0), db, user_id=uid)
    with _connect(db) as conn:
        history_before = conn.execute(
            "SELECT date, hevy_payload FROM workout_history WHERE user_id = ?", (uid,)
        ).fetchall()
    before = _progress_rows(db, uid)
    assert [row[0] for row in before] == ["2026-03-02", "2026-03-04"]

    result = update_user_settings(uid, {"timezone": "Pacific/Auckland"}, db)

    after = _progress_rows(db, uid)
    assert result.regrouped_days == 1  # only the 20:30Z workout crosses midnight
    assert [row[0] for row in after] == ["2026-03-03", "2026-03-04"]
    # Source timestamps and metrics are byte-for-byte unchanged.
    assert [row[1:] for row in after] == [row[1:] for row in before]
    with _connect(db) as conn:
        history_after = conn.execute(
            "SELECT date, hevy_payload FROM workout_history WHERE user_id = ?", (uid,)
        ).fetchall()
    assert history_after == history_before
    assert json.loads(history_after[0][1]) == payload


def test_regrouping_is_reversible_and_derived_only(db: str) -> None:
    uid = _user(db, "reverse@example.test")
    save_progress(_workout("2026-03-02T20:30:00+00:00"), db, user_id=uid)
    original = _progress_rows(db, uid)

    update_user_settings(uid, {"timezone": "Pacific/Auckland"}, db)
    assert _progress_rows(db, uid)[0][0] == "2026-03-03"
    result = update_user_settings(uid, {"timezone": "UTC"}, db)

    assert result.regrouped_days == 1
    assert _progress_rows(db, uid) == original


def test_session_groupings_follow_the_new_zone(db: str) -> None:
    uid = _user(db, "sessions@example.test")
    # Same UTC day, but two different days for someone in Auckland (UTC+13 in March).
    save_progress(_workout("2026-03-02T09:00:00+00:00", "Squat"), db, user_id=uid)
    save_progress(_workout("2026-03-02T20:30:00+00:00", "Bench"), db, user_id=uid)
    assert [s["date"] for s in get_session_volumes(db, user_id=uid)] == ["2026-03-02"]

    update_user_settings(uid, {"timezone": "Pacific/Auckland"}, db)

    assert [s["date"] for s in get_session_volumes(db, user_id=uid)] == [
        "2026-03-02",
        "2026-03-03",
    ]


@pytest.mark.parametrize(
    ("instant", "expected_day"),
    [
        # 00:30 EDT on the 9th (spring forward just happened); fixed EST says the 8th.
        ("2026-03-09T04:30:00+00:00", "2026-03-09"),
        # 23:30 EST on 1 Nov (fall back just happened); fixed EDT says the 2nd.
        ("2026-11-02T04:30:00+00:00", "2026-11-01"),
        # The repeated 01:30 hour of the fall-back night.
        ("2026-11-01T05:30:00+00:00", "2026-11-01"),
        ("2026-11-01T06:30:00+00:00", "2026-11-01"),
    ],
)
def test_regrouping_applies_the_dst_offset_in_force_at_each_instant(
    db: str, instant: str, expected_day: str
) -> None:
    uid = _user(db, "dst@example.test")
    save_progress(_workout(instant), db, user_id=uid)

    update_user_settings(uid, {"timezone": "America/New_York"}, db)

    assert _progress_rows(db, uid)[0][0] == expected_day


def test_regrouping_is_tenant_scoped(db: str) -> None:
    alice = _user(db, "alice-tz@example.test")
    bob = _user(db, "bob-tz@example.test")
    for uid in (alice, bob):
        save_progress(_workout("2026-03-02T20:30:00+00:00"), db, user_id=uid)
    bob_before = _progress_rows(db, bob)

    update_user_settings(alice, {"timezone": "Pacific/Auckland"}, db)

    assert _progress_rows(db, alice)[0][0] == "2026-03-03"
    assert _progress_rows(db, bob) == bob_before
    assert get_user_settings(bob, db).timezone == "UTC"


def test_rows_without_a_source_instant_keep_their_day(db: str) -> None:
    uid = _user(db, "dateonly@example.test")
    save_progress(_workout("2026-03-02"), db, user_id=uid)  # a bare calendar day

    result = update_user_settings(uid, {"timezone": "Pacific/Auckland"}, db)

    assert result.regrouped_days == 0
    assert _progress_rows(db, uid) == [("2026-03-02", None, 100.0)]


def test_non_time_zone_changes_do_not_regroup(db: str) -> None:
    uid = _user(db, "units@example.test")
    save_progress(_workout("2026-03-02T20:30:00+00:00"), db, user_id=uid)
    before = _progress_rows(db, uid)

    result = update_user_settings(uid, {"unit_system": "imperial", "locale": "en-US"}, db)

    assert result.regrouped_days == 0
    assert _progress_rows(db, uid) == before


def test_recompute_skips_unparseable_instants_and_other_tenants(db: str) -> None:
    uid = _user(db, "skip@example.test")
    other = _user(db, "other@example.test")
    save_progress(_workout("2026-03-02T20:30:00+00:00"), db, user_id=uid)
    save_progress(_workout("2026-03-02T20:30:00+00:00"), db, user_id=other)
    with _connect(db) as conn:
        conn.execute(
            "UPDATE exercise_progress SET performed_at = 'corrupt' WHERE user_id = ?", (uid,)
        )
        changed = recompute_local_days(conn, uid, "Pacific/Auckland")
        other_rows = conn.execute(
            "SELECT date FROM exercise_progress WHERE user_id = ?", (other,)
        ).fetchall()
    assert changed == 0
    assert other_rows == [("2026-03-02",)]


def test_a_failed_update_leaves_zone_and_groupings_untouched(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = _user(db, "rollback@example.test")
    save_progress(_workout("2026-03-02T20:30:00+00:00"), db, user_id=uid)

    def boom(*_args: object, **_kwargs: object) -> int:
        raise sqlite3.OperationalError("simulated failure mid-regroup")

    monkeypatch.setattr("user_settings.recompute_local_days", boom)
    with pytest.raises(sqlite3.OperationalError):
        update_user_settings(uid, {"timezone": "Pacific/Auckland"}, db)

    assert get_user_settings(uid, db).timezone == "UTC"
    assert _progress_rows(db, uid)[0][0] == "2026-03-02"


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def _legacy_schema(path: str, *rows: tuple[str, str]) -> None:
    """A database as it was before per-user settings existed."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, "
        "display_name TEXT, created_at TEXT NOT NULL, timezone TEXT DEFAULT 'UTC', "
        "units TEXT DEFAULT 'metric')"
    )
    conn.execute(
        "CREATE TABLE exercise_progress (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "date TEXT NOT NULL, exercise_name TEXT NOT NULL, top_weight_kg REAL, "
        "top_reps INTEGER, sets INTEGER NOT NULL, user_id TEXT)"
    )
    for user_id, zone in rows:
        conn.execute(
            "INSERT INTO users (id, email, created_at, timezone) VALUES (?, ?, 'x', ?)",
            (user_id, f"{user_id}@example.test", zone),
        )
    conn.commit()
    conn.close()


def _migrate(path: str, **kwargs: str | None) -> None:
    conn = sqlite3.connect(path)
    ensure_settings_schema(conn.cursor(), **kwargs)  # type: ignore[arg-type]
    conn.commit()
    conn.close()


def _zone_of(path: str, user_id: str) -> str:
    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("SELECT timezone FROM users WHERE id = ?", (user_id,)).fetchone()[0])
    finally:
        conn.close()


def test_migration_is_additive_and_idempotent(tmp_path: Path) -> None:
    path = str(tmp_path / "legacy.db")
    _legacy_schema(path, ("u1", "UTC"))

    _migrate(path, deployment_timezone="UTC")
    _migrate(path, deployment_timezone="UTC")

    conn = sqlite3.connect(path)
    user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    progress_cols = {row[1] for row in conn.execute("PRAGMA table_info(exercise_progress)")}
    conn.close()
    assert {"locale", "motion", "contrast", "text_size"} <= user_cols
    assert "performed_at" in progress_cols


def test_upgrade_moves_untouched_accounts_to_the_deployment_zone_once(tmp_path: Path) -> None:
    path = str(tmp_path / "legacy.db")
    _legacy_schema(path, ("owner", "UTC"), ("traveller", "Asia/Tokyo"))

    _migrate(path, deployment_timezone="Europe/London")

    assert _zone_of(path, "owner") == "Europe/London"  # keeps their old schedule
    assert _zone_of(path, "traveller") == "Asia/Tokyo"  # explicit choice is respected

    # Later a user chooses UTC on purpose; a re-run must not undo it.
    conn = sqlite3.connect(path)
    conn.execute("UPDATE users SET timezone = 'UTC' WHERE id = 'owner'")
    conn.commit()
    conn.close()
    _migrate(path, deployment_timezone="Europe/London")
    assert _zone_of(path, "owner") == "UTC"


@pytest.mark.parametrize("deployment_zone", ["", "UTC", "Mars/Base", "../../etc"])
def test_upgrade_ignores_a_missing_or_unusable_deployment_zone(
    tmp_path: Path, deployment_zone: str
) -> None:
    path = str(tmp_path / "legacy.db")
    _legacy_schema(path, ("owner", "UTC"))
    _migrate(path, deployment_timezone=deployment_zone)
    assert _zone_of(path, "owner") == "UTC"


def test_upgrade_reads_the_deployment_zone_from_tz_when_not_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", "Europe/London")
    path = str(tmp_path / "legacy.db")
    _legacy_schema(path, ("owner", "UTC"))
    _migrate(path)
    assert _zone_of(path, "owner") == "Europe/London"


def test_fresh_databases_are_never_seeded_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", "Europe/London")
    path = str(tmp_path / "fresh.db")
    init_db(path)
    assert get_user_settings(get_or_create_user("f@example.test", None, path)["id"], path) == (
        UserSettings()
    )


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


def test_export_preferences_tell_a_reader_how_to_interpret_the_data() -> None:
    block = export_preferences(
        UserSettings(timezone="Europe/London", unit_system="imperial", motion="reduce")
    )
    assert block["timezone"] == "Europe/London"
    assert block["unit_system"] == "imperial"
    assert block["weight_unit"] == "lb"
    assert block["stored_weight_unit"] == "kg"
    assert "UTC" in block["timestamps"]
    assert block["motion"] == "reduce"
