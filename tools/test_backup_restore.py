from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from tools.backup_restore import (
    BackupError,
    Keyring,
    create_backup,
    load_keyring,
    restore_backup,
    retention_candidates,
    verify_backup,
)


def _make_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE workouts ("
            "id INTEGER PRIMARY KEY, "
            "user_id TEXT NOT NULL, "
            "note TEXT, "
            "FOREIGN KEY(user_id) REFERENCES users(id))"
        )
        conn.execute(
            "INSERT INTO users VALUES (?, ?)",
            ("synthetic-user", "synthetic@example.invalid"),
        )
        conn.execute(
            "INSERT INTO workouts(user_id, note) VALUES (?, ?)",
            ("synthetic-user", "synthetic sensitive training note"),
        )
        conn.commit()


def _keyring(key_id: str = "k1") -> tuple[Keyring, bytes]:
    key = Fernet.generate_key()
    return Keyring({key_id: key}, key_id), key


def test_round_trip_is_encrypted_and_private(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    _make_db(database)
    keyring, _ = _keyring()
    backup = tmp_path / "backup.wab"

    manifest = create_backup(
        database,
        backup,
        keyring,
        created_at="2026-08-20T20:00:00Z",
    )

    raw = backup.read_bytes()
    assert b"synthetic@example.invalid" not in raw
    assert b"synthetic sensitive training note" not in raw
    assert manifest["table_counts"] == {"users": 1, "workouts": 1}
    assert backup.stat().st_mode & 0o777 == 0o600

    restored = tmp_path / "restored.db"
    restore_backup(backup, restored, keyring)
    with sqlite3.connect(restored) as conn:
        email = conn.execute("SELECT email FROM users").fetchone()[0]
    assert email == "synthetic@example.invalid"
    assert verify_backup(backup, keyring)["database_sha256"] == manifest[
        "database_sha256"
    ]


def test_keyring_configuration_fails_closed() -> None:
    with pytest.raises(BackupError):
        load_keyring("")
    with pytest.raises(BackupError):
        load_keyring("missing-separator")
    with pytest.raises(BackupError):
        load_keyring("k:not-a-fernet-key")


def test_rotation_keeps_old_backups_readable(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    _make_db(database)
    old_key = Fernet.generate_key()
    new_key = Fernet.generate_key()
    old_keyring = Keyring({"2026-01": old_key}, "2026-01")
    backup = tmp_path / "old.wab"
    create_backup(database, backup, old_keyring)

    rotated_keyring = Keyring(
        {"2026-01": old_key, "2026-08": new_key},
        "2026-08",
    )
    assert verify_backup(backup, rotated_keyring)["key_id"] == "2026-01"


def test_wrong_or_tampered_key_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    _make_db(database)
    keyring, _ = _keyring()
    backup = tmp_path / "backup.wab"
    create_backup(database, backup, keyring)

    wrong_keyring = Keyring({"k1": Fernet.generate_key()}, "k1")
    with pytest.raises(BackupError, match="authentication failed"):
        verify_backup(backup, wrong_keyring)

    payload = bytearray(backup.read_bytes())
    payload[-1] ^= 1
    backup.write_bytes(payload)
    with pytest.raises(BackupError, match="authentication failed"):
        verify_backup(backup, keyring)


def test_restore_refuses_to_overwrite_database(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    _make_db(database)
    keyring, _ = _keyring()
    backup = tmp_path / "backup.wab"
    create_backup(database, backup, keyring)

    target = tmp_path / "existing.db"
    target.write_text("keep me", encoding="utf-8")
    with pytest.raises(BackupError, match="Refusing to overwrite"):
        restore_backup(backup, target, keyring)
    assert target.read_text(encoding="utf-8") == "keep me"


def test_retention_keeps_minimum_latest_and_skips_invalid(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    _make_db(database)
    keyring, _ = _keyring()
    timestamps = [
        "2026-06-01T00:00:00Z",
        "2026-06-10T00:00:00Z",
        "2026-07-01T00:00:00Z",
        "2026-08-19T00:00:00Z",
    ]
    backups: list[Path] = []
    for index, timestamp in enumerate(timestamps):
        path = tmp_path / f"backup-{index}.wab"
        create_backup(database, path, keyring, created_at=timestamp)
        backups.append(path)

    invalid = tmp_path / "invalid.wab"
    invalid.write_bytes(b"not-a-backup")
    candidates, skipped = retention_candidates(
        tmp_path,
        max_age_days=35,
        minimum_latest=2,
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert set(candidates) == set(backups[:2])
    assert skipped == [invalid]


def test_foreign_key_violation_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "invalid.db"
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE child(pid INTEGER REFERENCES parent(id))")
        conn.execute("INSERT INTO child VALUES (9)")
        conn.commit()

    keyring, _ = _keyring()
    with pytest.raises(BackupError, match="foreign_key_check"):
        create_backup(database, tmp_path / "invalid.wab", keyring)
