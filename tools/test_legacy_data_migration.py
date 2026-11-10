"""Tests for the one-time legacy single-user data migration tool.

All fixtures are synthetic. The database is created by the production
``database.init_db()`` so the tests exercise the real tenant schema.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

from tools import legacy_data_migration as migration
from tools.backup_restore import BackupError, Keyring, restore_backup, verify_backup
from tools.legacy_data_migration import (
    EPHEMERAL_TABLES,
    USER_OWNED_TABLES,
    MigrationError,
    migrate,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import database

SYNTHETIC_VALUES: dict[str, Any] = {
    "TEXT": "synthetic",
    "INTEGER": 1,
    "REAL": 1.0,
    "BLOB": b"synthetic",
}
JSON_COLUMNS = {"hevy_payload": "{}", "insight_json": "{}"}
# Nullable columns that take part in a unique key must be set, because NULLs
# never collide.
TABLE_DEFAULTS: dict[str, dict[str, Any]] = {
    "programmes": {"template_key": "synthetic"}
}
Snapshot = dict[str, list[tuple[Any, ...]]]


@contextlib.contextmanager
def _db(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path, timeout=10)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _insert_row(
    conn: sqlite3.Connection, table: str, user_id: str | None, **overrides: Any
) -> bool:
    """Insert one synthetic row; return False when an existing key ignored it."""
    overrides = {**TABLE_DEFAULTS.get(table, {}), **overrides}
    columns = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    primary_key = [column for column in columns if column[5]]
    row: dict[str, Any] = {}
    for _cid, name, column_type, not_null, default, pk in columns:
        if name == "user_id":
            row[name] = user_id
        elif name in overrides:
            row[name] = overrides[name]
        elif name in JSON_COLUMNS:
            row[name] = JSON_COLUMNS[name]
        elif pk and column_type.upper() == "INTEGER" and len(primary_key) == 1:
            continue  # rowid alias
        elif not_null and default is None:
            row[name] = SYNTHETIC_VALUES.get(column_type.upper(), "synthetic")
    names = ", ".join(f'"{name}"' for name in row)
    marks = ", ".join("?" for _ in row)
    cursor = conn.execute(
        f'INSERT OR IGNORE INTO "{table}" ({names}) VALUES ({marks})',
        tuple(row.values()),
    )
    return cursor.rowcount == 1


def _snapshot(path: Path) -> Snapshot:
    with _db(path) as conn:
        return {
            table: [
                tuple(row)
                for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
            ]
            for table in ("users", *USER_OWNED_TABLES)
        }


def _legacy_id(path: Path) -> str:
    with _db(path) as conn:
        return conn.execute(
            "SELECT id FROM users WHERE email = ?", (migration.LEGACY_EMAIL,)
        ).fetchone()[0]


def _insert_unowned(conn: sqlite3.Connection, table: str, **overrides: Any) -> bool:
    """Insert a pre-launch unowned row, which the owner guard now forbids."""
    conn.execute(f"DROP TRIGGER IF EXISTS trg_{table}_owner_required_insert")
    try:
        return _insert_row(conn, table, None, **overrides)
    finally:
        database._install_owner_guards(conn.cursor())


def _owners(path: Path, table: str) -> set[str | None]:
    with _db(path) as conn:
        return {row[0] for row in conn.execute(f'SELECT user_id FROM "{table}"')}


def _legacy_rows(path: Path, legacy_id: str) -> dict[str, int]:
    with _db(path) as conn:
        return {
            table: conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE user_id IS NULL OR user_id = ?',
                (legacy_id,),
            ).fetchone()[0]
            for table in USER_OWNED_TABLES
        }


def _new_account(path: Path, email: str) -> str:
    return database.get_or_create_user(email, "Synthetic", db_path=str(path))["id"]


def _visible_records(path: Path, user_id: str) -> dict[str, bool]:
    """Ask the production read API what ``user_id`` can see."""
    db = str(path)
    return {
        "workout_history": bool(
            database.get_recent_hevy_logs(db_path=db, user_id=user_id)
        ),
        "exercise_progress": bool(
            database.get_progress_history(db_path=db, user_id=user_id)
        ),
        "recent_bests": bool(database.get_recent_bests(db_path=db, user_id=user_id)),
        "daily_log": bool(database.get_daily_logs(db_path=db, user_id=user_id)),
        "body_metrics": bool(database.get_body_metrics(db_path=db, user_id=user_id)),
        "check_ins": bool(database.get_checkins(db_path=db, user_id=user_id)),
        "chat_messages": bool(database.get_chat_messages(db_path=db, user_id=user_id)),
        "dashboard_insights": database.get_dashboard_insight(
            db_path=db, user_id=user_id
        )
        is not None,
        "reasoning_logs": database.get_reasoning_log(
            "synthetic", db_path=db, user_id=user_id
        )
        is not None,
        "deep_correlations": database.get_deep_correlation(db_path=db, user_id=user_id)
        is not None,
        "hevy_routines": database.get_routine_record(
            "synthetic", db_path=db, user_id=user_id
        )
        is not None,
        "hevy_meta": database.get_meta("synthetic", db, user_id=user_id) is not None,
        "notifications": bool(database.get_notifications(user_id, db_path=db)),
        "push_subscriptions": bool(
            database.get_push_subscriptions(user_id, db_path=db)
        ),
        "user_api_keys": bool(database.get_user_api_keys(user_id, db_path=db)),
        "programmes": database.get_active_programme(user_id, db_path=db) is not None,
    }


@pytest.fixture(autouse=True)
def _pre_launch_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build the pre-launch schema: ``init_db`` seeds the legacy tenant only here."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ALLOW_ANONYMOUS_WEB", "true")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())


@pytest.fixture()
def keyring() -> Keyring:
    return Keyring(keys={"test": Fernet.generate_key()}, active_key_id="test")


@pytest.fixture()
def legacy_db(tmp_path: Path) -> tuple[Path, str, str]:
    """A tenant-schema database with legacy rows in every user-owned table."""
    path = tmp_path / "workout_agent.db"
    database.init_db(str(path))
    legacy_id = _legacy_id(path)
    target_id = _new_account(path, "owner@example.invalid")
    with _db(path) as conn:
        for table in USER_OWNED_TABLES:
            _insert_row(conn, table, legacy_id)
        _insert_unowned(conn, "daily_log", date="2030-01-02")  # unowned row
        conn.execute("UPDATE programmes SET active = 1 WHERE user_id = ?", (legacy_id,))
    return path, legacy_id, target_id


def _run(path: Path, target_id: str, backup: Path, keyring: Keyring) -> dict[str, Any]:
    return migrate(
        path, target_user_id=target_id, backup_output=backup, keyring=keyring
    )


def _assert_refused_untouched(
    path: Path,
    backup: Path,
    before: Snapshot,
    error: type[Exception],
    match: str,
    target_id: str,
    keyring: Keyring,
) -> None:
    with pytest.raises(error, match=match):
        _run(path, target_id, backup, keyring)
    assert _snapshot(path) == before
    assert not backup.exists()


def test_user_owned_tables_cover_every_tenant_scoped_table(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    database.init_db(str(path))
    with _db(path) as conn:
        tenant_tables = {
            name
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'users'"
            )
            if any(
                column[1] == "user_id"
                for column in conn.execute(f'PRAGMA table_info("{name}")')
            )
        }
    assert tenant_tables == set(USER_OWNED_TABLES) | set(EPHEMERAL_TABLES) - {
        "csrf_tokens"
    }


def test_init_db_reseeds_only_the_defaults_the_tool_knows_about(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    database.init_db(str(path))
    legacy_id = _legacy_id(path)
    seeded = {table: n for table, n in _legacy_rows(path, legacy_id).items() if n}
    assert seeded == {"programme_state": 1, "hevy_meta": 1}
    with _db(path) as conn:
        key = conn.execute("SELECT key FROM hevy_meta").fetchone()[0]
    assert key == "programme_start_date"


@pytest.mark.parametrize("target", ["", "   ", "missing-account"])
def test_requires_an_explicit_existing_target(
    legacy_db: tuple[Path, str, str], keyring: Keyring, tmp_path: Path, target: str
) -> None:
    path, _legacy_id, _target_id = legacy_db
    _assert_refused_untouched(
        path,
        tmp_path / "backup.wab",
        _snapshot(path),
        MigrationError,
        "explicit target|does not exist",
        target,
        keyring,
    )


def test_refuses_the_legacy_tenant_as_target(
    legacy_db: tuple[Path, str, str], keyring: Keyring, tmp_path: Path
) -> None:
    path, legacy_id, _target_id = legacy_db
    _assert_refused_untouched(
        path,
        tmp_path / "backup.wab",
        _snapshot(path),
        MigrationError,
        "legacy tenant",
        legacy_id,
        keyring,
    )


def test_refuses_a_missing_database(keyring: Keyring, tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="does not exist"):
        _run(tmp_path / "absent.db", "any", tmp_path / "backup.wab", keyring)
    assert not (tmp_path / "absent.db").exists()


def test_adopts_every_legacy_and_unowned_row(
    legacy_db: tuple[Path, str, str], keyring: Keyring, tmp_path: Path
) -> None:
    path, legacy_id, target_id = legacy_db
    expected = _legacy_rows(path, legacy_id)
    assert all(count > 0 for count in expected.values())
    assert expected["daily_log"] == 2  # one legacy-owned row and one unowned row

    evidence = _run(path, target_id, tmp_path / "pre.wab", keyring)

    assert evidence["status"] == "migrated"
    assert evidence["rows_reassigned"] == sum(expected.values())
    assert evidence["legacy_user_id"] == legacy_id
    assert evidence["integrity_before"] == {
        "integrity_check": "ok",
        "foreign_key_violations": 0,
    }
    assert evidence["integrity_after"] == {
        "integrity_check": "ok",
        "foreign_key_violations": 0,
    }
    for table in USER_OWNED_TABLES:
        before, after = evidence["before"][table], evidence["after"][table]
        assert before == {
            "total": expected[table],
            "legacy": expected[table],
            "target": 0,
            "other": 0,
        }
        assert after == {
            "total": expected[table],
            "legacy": 0,
            "target": expected[table],
            "other": 0,
        }
        assert _owners(path, table) == {target_id}, table
    assert evidence["retained_default_rows"] == {}
    assert evidence["merged_tables"] == []
    with _db(path) as conn:  # the legacy tenant itself is left in place, now empty
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM users WHERE id = ?", (legacy_id,)
            ).fetchone()[0]
            == 1
        )


def test_adopts_unowned_rows_when_no_legacy_tenant_exists(
    keyring: Keyring, tmp_path: Path
) -> None:
    path = tmp_path / "no_legacy.db"
    database.init_db(str(path))
    target_id = _new_account(path, "owner@example.invalid")
    with _db(path) as conn:
        conn.execute("DELETE FROM programme_state")
        conn.execute("DELETE FROM hevy_meta")
        conn.execute("DELETE FROM users WHERE email = 'legacy@local'")
        assert _insert_unowned(conn, "daily_log")

    evidence = _run(path, target_id, tmp_path / "pre.wab", keyring)

    assert evidence["legacy_user_id"] is None
    assert evidence["rows_reassigned"] == 1
    assert _owners(path, "daily_log") == {target_id}
    assert _run(path, target_id, tmp_path / "again.wab", keyring)["status"] == "noop"


def test_backup_is_verified_private_and_restores_the_pre_migration_state(
    legacy_db: tuple[Path, str, str], keyring: Keyring, tmp_path: Path
) -> None:
    path, _legacy_id, target_id = legacy_db
    pre_migration = _snapshot(path)
    backup = tmp_path / "pre.wab"

    evidence = _run(path, target_id, backup, keyring)

    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    manifest = verify_backup(backup, keyring)
    assert evidence["backup"] == {
        "path": str(backup),
        "created_at": manifest["created_at"],
        "key_id": "test",
        "database_sha256": manifest["database_sha256"],
    }
    assert _snapshot(path) != pre_migration
    restored = tmp_path / "restored.db"
    restore_backup(backup, restored, keyring)
    assert _snapshot(restored) == pre_migration  # rollback by restore is exact


def test_second_run_is_a_noop(
    legacy_db: tuple[Path, str, str], keyring: Keyring, tmp_path: Path
) -> None:
    path, _legacy_id, target_id = legacy_db
    _run(path, target_id, tmp_path / "first.wab", keyring)
    migrated = _snapshot(path)

    second = _run(path, target_id, tmp_path / "second.wab", keyring)

    assert second["status"] == "noop"
    assert second["rows_reassigned"] == 0
    assert second["backup"] is None
    assert second["before"] == second["after"]
    assert _snapshot(path) == migrated
    assert not (tmp_path / "second.wab").exists()  # nothing changed, nothing to back up


def test_rerun_after_restart_leaves_reseeded_defaults_with_the_legacy_tenant(
    legacy_db: tuple[Path, str, str], keyring: Keyring, tmp_path: Path
) -> None:
    path, legacy_id, target_id = legacy_db
    _run(path, target_id, tmp_path / "first.wab", keyring)
    database.init_db(str(path))  # application restart re-seeds the legacy defaults
    assert {t: n for t, n in _legacy_rows(path, legacy_id).items() if n} == {
        "programme_state": 1,
        "hevy_meta": 1,
    }
    restarted = _snapshot(path)

    second = _run(path, target_id, tmp_path / "second.wab", keyring)

    assert second["status"] == "noop"
    assert second["retained_default_rows"] == {"hevy_meta": 1, "programme_state": 1}
    assert _snapshot(path) == restarted
    assert not (tmp_path / "second.wab").exists()


def test_new_accounts_cannot_see_legacy_records(
    legacy_db: tuple[Path, str, str], keyring: Keyring, tmp_path: Path
) -> None:
    path, legacy_id, target_id = legacy_db
    new_account = _new_account(path, "second@example.invalid")
    assert all(_visible_records(path, legacy_id).values())  # readers do see legacy data
    assert not any(_visible_records(path, target_id).values())

    _run(path, target_id, tmp_path / "pre.wab", keyring)
    later_account = _new_account(path, "later@example.invalid")

    assert all(_visible_records(path, target_id).values())
    for account in (new_account, later_account, legacy_id):
        assert not any(_visible_records(path, account).values()), account
    # No row is left that is unowned, legacy-owned or owned by anyone else.
    for table in USER_OWNED_TABLES:
        assert _owners(path, table) == {target_id}, table


def test_api_keys_stay_decryptable_by_the_target(
    legacy_db: tuple[Path, str, str],
    keyring: Keyring,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    path, legacy_id, target_id = legacy_db
    database.save_user_api_key(
        legacy_id, "gemini", "synthetic-secret-value", db_path=str(path)
    )

    evidence = _run(path, target_id, tmp_path / "pre.wab", keyring)

    record = database.get_user_api_key(target_id, "gemini", db_path=str(path))
    assert record is not None and record["api_key"] == "synthetic-secret-value"
    assert database.get_user_api_key(legacy_id, "gemini", db_path=str(path)) is None
    assert "synthetic-secret-value" not in json.dumps(evidence)


def test_refuses_rows_owned_by_another_account(
    legacy_db: tuple[Path, str, str], keyring: Keyring, tmp_path: Path
) -> None:
    path, _legacy_id, target_id = legacy_db
    other_id = _new_account(path, "other@example.invalid")
    with _db(path) as conn:
        assert _insert_row(conn, "workout_history", other_id)
    _assert_refused_untouched(
        path,
        tmp_path / "backup.wab",
        _snapshot(path),
        MigrationError,
        r"Ambiguous ownership.*workout_history \(1\)",
        target_id,
        keyring,
    )


def test_refuses_rows_owned_by_an_unknown_account(
    legacy_db: tuple[Path, str, str], keyring: Keyring, tmp_path: Path
) -> None:
    path, _legacy_id, target_id = legacy_db
    with _db(path) as conn:  # foreign keys are not enforced by the application
        assert _insert_row(conn, "chat_messages", "deleted-account-id")
    _assert_refused_untouched(
        path,
        tmp_path / "backup.wab",
        _snapshot(path),
        MigrationError,
        r"foreign_key_check.*chat_messages \(1\)",
        target_id,
        keyring,
    )


@pytest.mark.parametrize(
    "table",
    [
        "user_api_keys",
        "programmes",
        "user_preferences",
        "hevy_routines",
        "reasoning_logs",
        "dashboard_insights",
        "deep_correlations",
        "hevy_meta",
    ],
)
def test_refuses_legacy_rows_that_collide_with_the_targets_rows(
    legacy_db: tuple[Path, str, str], keyring: Keyring, tmp_path: Path, table: str
) -> None:
    path, _legacy_id, target_id = legacy_db
    with _db(path) as conn:
        assert _insert_row(conn, table, target_id)  # same synthetic unique key
    _assert_refused_untouched(
        path,
        tmp_path / "backup.wab",
        _snapshot(path),
        MigrationError,
        rf"Ambiguous ownership.*collide.*{table} \(1\)",
        target_id,
        keyring,
    )


def test_sqlite_constraint_failure_rolls_back_and_refuses(
    legacy_db: tuple[Path, str, str],
    keyring: Keyring,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _legacy_id, target_id = legacy_db
    with _db(path) as conn:
        assert _insert_row(conn, "user_api_keys", target_id)
    before = _snapshot(path)
    monkeypatch.setattr(migration, "_ownership_conflicts", lambda *_args: ({}, {}))

    with pytest.raises(MigrationError, match="Ownership conflict while adopting"):
        _run(path, target_id, tmp_path / "backup.wab", keyring)

    assert _snapshot(path) == before  # the transaction rolled back completely


def test_target_with_an_active_programme_keeps_exactly_one(
    legacy_db: tuple[Path, str, str], keyring: Keyring, tmp_path: Path
) -> None:
    path, legacy_id, target_id = legacy_db
    with _db(path) as conn:
        assert _insert_row(conn, "programmes", target_id, template_key="own", active=1)

    evidence = _run(path, target_id, tmp_path / "pre.wab", keyring)

    assert evidence["programmes_deactivated"] == 1
    active = database.get_active_programme(target_id, db_path=str(path))
    assert active is not None and active["template_key"] == "own"
    with _db(path) as conn:
        rows = conn.execute(
            "SELECT template_key, active FROM programmes WHERE user_id = ? ORDER BY id",
            (target_id,),
        ).fetchall()
    assert sorted(rows) == [("own", 1), ("synthetic", 0)]
    assert _visible_records(path, legacy_id)["programmes"] is False


def test_adoption_into_existing_history_is_reported_not_hidden(
    legacy_db: tuple[Path, str, str], keyring: Keyring, tmp_path: Path
) -> None:
    path, _legacy_id, target_id = legacy_db
    with _db(path) as conn:
        assert _insert_row(conn, "workout_history", target_id)

    evidence = _run(path, target_id, tmp_path / "pre.wab", keyring)

    assert evidence["merged_tables"] == ["workout_history"]
    assert evidence["after"]["workout_history"]["target"] == 2
    assert evidence["after"]["workout_history"]["total"] == 2


def test_refuses_a_schema_with_an_uncovered_tenant_table(
    legacy_db: tuple[Path, str, str], keyring: Keyring, tmp_path: Path
) -> None:
    path, _legacy_id, target_id = legacy_db
    with _db(path) as conn:
        conn.execute(
            "CREATE TABLE sync_cursors "
            "(id INTEGER PRIMARY KEY, user_id TEXT REFERENCES users(id))"
        )
    _assert_refused_untouched(
        path,
        tmp_path / "backup.wab",
        _snapshot(path),
        MigrationError,
        "does not adopt.*sync_cursors",
        target_id,
        keyring,
    )


def test_refuses_a_schema_that_is_not_tenant_scoped(
    keyring: Keyring, tmp_path: Path
) -> None:
    path = tmp_path / "pre_tenant.db"
    with _db(path) as conn:
        conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT NOT NULL)")
        conn.execute("INSERT INTO users VALUES ('owner', 'owner@example.invalid')")
        conn.execute(
            "CREATE TABLE workout_history (id INTEGER PRIMARY KEY, hevy_payload TEXT)"
        )
    with pytest.raises(
        MigrationError, match="without a user_id column.*workout_history"
    ):
        _run(path, "owner", tmp_path / "backup.wab", keyring)
    assert not (tmp_path / "backup.wab").exists()


def test_failed_post_migration_verification_rolls_everything_back(
    legacy_db: tuple[Path, str, str],
    keyring: Keyring,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _legacy_id, target_id = legacy_db
    before = _snapshot(path)

    def fail(*_args: Any) -> None:
        raise MigrationError("verification failed")

    monkeypatch.setattr(migration, "_verify_after", fail)
    with pytest.raises(MigrationError, match="verification failed"):
        _run(path, target_id, tmp_path / "pre.wab", keyring)

    assert _snapshot(path) == before
    verify_backup(tmp_path / "pre.wab", keyring)  # the verified backup still exists


def test_backup_exists_before_the_first_row_changes(
    legacy_db: tuple[Path, str, str],
    keyring: Keyring,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _legacy_id, target_id = legacy_db
    backup = tmp_path / "pre.wab"
    backup_present_at: dict[str, bool] = {}

    def spy(name: str) -> None:
        real = getattr(migration, name)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            backup_present_at[name] = backup.exists()
            return real(*args, **kwargs)

        monkeypatch.setattr(migration, name, wrapper)

    spy("_supersede_legacy_programmes")
    spy("_reassign")
    _run(path, target_id, backup, keyring)

    assert backup_present_at == {
        "_supersede_legacy_programmes": True,
        "_reassign": True,
    }


def test_refuses_a_backup_that_does_not_match_the_database(
    legacy_db: tuple[Path, str, str],
    keyring: Keyring,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _legacy_id, target_id = legacy_db
    before = _snapshot(path)
    real = migration.create_backup

    def stale(*args: Any, **kwargs: Any) -> dict[str, Any]:
        manifest = real(*args, **kwargs)
        manifest["table_counts"] = {**manifest["table_counts"], "workout_history": 99}
        return manifest

    monkeypatch.setattr(migration, "create_backup", stale)
    _assert_refused_untouched(
        path,
        tmp_path / "pre.wab",
        before,
        MigrationError,
        "Backup row count for workout_history",
        target_id,
        keyring,
    )


def test_backup_failure_prevents_any_change(
    legacy_db: tuple[Path, str, str],
    keyring: Keyring,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _legacy_id, target_id = legacy_db
    before = _snapshot(path)

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise BackupError("disk full")

    monkeypatch.setattr(migration, "create_backup", fail)
    with pytest.raises(BackupError, match="disk full"):
        _run(path, target_id, tmp_path / "pre.wab", keyring)
    assert _snapshot(path) == before


def test_unverifiable_backup_is_discarded_and_nothing_changes(
    legacy_db: tuple[Path, str, str],
    keyring: Keyring,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _legacy_id, target_id = legacy_db
    before = _snapshot(path)

    def fail(*_args: Any) -> None:
        raise BackupError("authentication failed")

    monkeypatch.setattr(migration, "verify_backup", fail)
    _assert_refused_untouched(
        path,
        tmp_path / "pre.wab",
        before,
        BackupError,
        "authentication failed",
        target_id,
        keyring,
    )


def test_never_overwrites_an_existing_backup(
    legacy_db: tuple[Path, str, str], keyring: Keyring, tmp_path: Path
) -> None:
    path, _legacy_id, target_id = legacy_db
    before = _snapshot(path)
    existing = tmp_path / "pre.wab"
    existing.write_bytes(b"earlier backup")

    with pytest.raises(BackupError, match="Refusing to overwrite"):
        _run(path, target_id, existing, keyring)

    assert existing.read_bytes() == b"earlier backup"
    assert _snapshot(path) == before


def test_refuses_while_another_writer_holds_the_database(
    legacy_db: tuple[Path, str, str],
    keyring: Keyring,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _legacy_id, target_id = legacy_db
    before = _snapshot(path)
    monkeypatch.setattr(migration, "LOCK_TIMEOUT_SECONDS", 0.2)
    writer = sqlite3.connect(path, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(MigrationError, match="exclusive write lock"):
            _run(path, target_id, tmp_path / "pre.wab", keyring)
    finally:
        writer.close()
    assert _snapshot(path) == before
    assert not (tmp_path / "pre.wab").exists()


def _cli(*args: str, key: str | None, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in {"BACKUP_ENCRYPTION_KEYS", "BACKUP_ACTIVE_KEY_ID"}
    }
    if key is not None:
        env["BACKUP_ENCRYPTION_KEYS"] = f"cli:{key}"
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "legacy_data_migration.py"), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        check=False,
        timeout=120,
    )


def test_cli_migrates_reports_counts_and_writes_private_evidence(
    legacy_db: tuple[Path, str, str], tmp_path: Path
) -> None:
    path, _legacy_id, target_id = legacy_db
    key = Fernet.generate_key().decode()
    evidence = tmp_path / "evidence" / "adoption.json"
    arguments = (
        "migrate",
        "--database",
        str(path),
        "--target-user-id",
        target_id,
        "--backup-output",
        str(tmp_path / "pre.wab"),
        "--evidence",
        str(evidence),
    )

    result = _cli(*arguments, key=key, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Legacy data adoption: migrated" in result.stdout
    assert "Rows reassigned:" in result.stdout
    assert "Integrity: ok -> ok" in result.stdout
    assert key not in result.stdout + result.stderr
    assert stat.S_IMODE(evidence.stat().st_mode) == 0o600
    document = json.loads(evidence.read_text(encoding="utf-8"))
    assert document["status"] == "migrated"
    assert document["target_user_id"] == target_id
    assert "example.invalid" not in evidence.read_text(encoding="utf-8")
    for table in USER_OWNED_TABLES:
        assert _owners(path, table) == {target_id}

    # A repeat is refused rather than overwriting the earlier evidence file.
    repeat = _cli(*arguments, key=key, cwd=tmp_path)
    assert repeat.returncode == 1
    assert "refusing to overwrite evidence" in repeat.stderr
    assert json.loads(evidence.read_text(encoding="utf-8")) == document


def test_cli_requires_explicit_target_and_backup(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = migration.build_parser()
    for arguments in (
        ["migrate", "--database", "x.db", "--backup-output", "b.wab"],
        ["migrate", "--database", "x.db", "--target-user-id", "t"],
    ):
        with pytest.raises(SystemExit) as exit_info:
            parser.parse_args(arguments)
        assert exit_info.value.code == 2
    capsys.readouterr()


def test_cli_fails_closed_without_backup_keys(
    legacy_db: tuple[Path, str, str], tmp_path: Path
) -> None:
    path, _legacy_id, target_id = legacy_db
    before = _snapshot(path)

    result = _cli(
        "migrate",
        "--database",
        str(path),
        "--target-user-id",
        target_id,
        "--backup-output",
        str(tmp_path / "pre.wab"),
        key=None,
        cwd=tmp_path,
    )

    assert result.returncode == 2
    assert "BACKUP_ENCRYPTION_KEYS is required" in result.stderr
    assert _snapshot(path) == before


def test_cli_reports_a_refusal_with_a_nonzero_exit_status(
    legacy_db: tuple[Path, str, str], tmp_path: Path
) -> None:
    path, _legacy_id, _target_id = legacy_db
    before = _snapshot(path)

    result = _cli(
        "migrate",
        "--database",
        str(path),
        "--target-user-id",
        "missing-account",
        "--backup-output",
        str(tmp_path / "pre.wab"),
        key=Fernet.generate_key().decode(),
        cwd=tmp_path,
    )

    assert result.returncode == 1
    assert "migration error:" in result.stderr
    assert "does not exist" in result.stderr
    assert _snapshot(path) == before
    assert not (tmp_path / "pre.wab").exists()
