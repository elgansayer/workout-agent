#!/usr/bin/env python3
"""Encrypted SQLite backups and restore verification for Workout Agent.

Backups fail closed unless a dedicated Fernet keyring is configured. The SQLite
online-backup API is used so WAL-backed production databases are copied from a
consistent snapshot without copying ``-wal``/``-shm`` files by hand.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sqlite3
import struct
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken

FORMAT_VERSION = 1
MAGIC = b"WORKOUT_AGENT_BACKUP_V1\n"
HEADER_LIMIT = 4096
KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ARCHIVE_DATABASE = "database.sqlite3"
ARCHIVE_MANIFEST = "manifest.json"


class BackupError(RuntimeError):
    """Raised when a backup cannot be created or verified safely."""


@dataclass(frozen=True)
class Keyring:
    keys: Mapping[str, bytes]
    active_key_id: str

    @property
    def active_key(self) -> bytes:
        return self.keys[self.active_key_id]


def utc_now() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_utc(value: str) -> datetime:
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        raise BackupError("Backup timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def load_keyring(
    raw: str | None = None,
    *,
    active_key_id: str | None = None,
) -> Keyring:
    """Load ``key-id:fernet-key`` entries, failing closed on any ambiguity."""
    value = raw if raw is not None else os.environ.get("BACKUP_ENCRYPTION_KEYS", "")
    value = value.strip()
    if not value:
        raise BackupError(
            "BACKUP_ENCRYPTION_KEYS is required; refusing to create or decrypt an unencrypted backup"
        )

    parsed: dict[str, bytes] = {}
    for item in value.split(","):
        entry = item.strip()
        if not entry or ":" not in entry:
            raise BackupError("Invalid BACKUP_ENCRYPTION_KEYS entry")
        key_id, encoded = entry.split(":", 1)
        key_id = key_id.strip()
        encoded = encoded.strip()
        if not KEY_ID_RE.fullmatch(key_id):
            raise BackupError(f"Invalid backup key id: {key_id!r}")
        if key_id in parsed:
            raise BackupError(f"Duplicate backup key id: {key_id}")
        try:
            Fernet(encoded.encode("ascii"))
        except Exception as exc:  # noqa: BLE001
            raise BackupError(f"Invalid Fernet key for backup key id {key_id}") from exc
        parsed[key_id] = encoded.encode("ascii")

    selected = (
        active_key_id
        or os.environ.get("BACKUP_ACTIVE_KEY_ID", "").strip()
        or next(iter(parsed))
    )
    if selected not in parsed:
        raise BackupError(f"Active backup key id {selected!r} is not present in the keyring")
    return Keyring(keys=parsed, active_key_id=selected)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _database_snapshot(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise BackupError(f"SQLite database does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source.resolve()}?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True, timeout=30) as src:
            src.execute("PRAGMA query_only=ON")
            with sqlite3.connect(destination) as dst:
                src.backup(dst)
                dst.commit()
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite online backup failed: {exc}") from exc


def _database_metadata(path: Path) -> dict[str, Any]:
    try:
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
            integrity_rows = [row[0] for row in conn.execute("PRAGMA integrity_check")]
            if integrity_rows != ["ok"]:
                raise BackupError("SQLite integrity_check failed")
            foreign_key_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_rows:
                raise BackupError(
                    f"SQLite foreign_key_check reported {len(foreign_key_rows)} violation(s)"
                )
            objects = conn.execute(
                """
                SELECT type, name, tbl_name, COALESCE(sql, '')
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            ).fetchall()
            schema_payload = json.dumps(
                objects, separators=(",", ":"), ensure_ascii=True
            ).encode()
            tables = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                    """
                )
            ]
            counts = {
                table: int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                    ).fetchone()[0]
                )
                for table in tables
            }
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
    except sqlite3.Error as exc:
        raise BackupError(f"Could not inspect SQLite database: {exc}") from exc
    return {
        "schema_sha256": _sha256(schema_payload),
        "table_counts": counts,
        "sqlite_user_version": user_version,
        "sqlite_application_id": application_id,
        "integrity_check": "ok",
        "foreign_key_violations": 0,
    }


def _archive_bytes(database_bytes: bytes, manifest: Mapping[str, Any]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        archive.writestr(
            ARCHIVE_MANIFEST,
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        )
        archive.writestr(ARCHIVE_DATABASE, database_bytes)
    return buffer.getvalue()


def _extract_archive(payload: bytes) -> tuple[dict[str, Any], bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(payload), mode="r") as archive:
            names = set(archive.namelist())
            expected = {ARCHIVE_MANIFEST, ARCHIVE_DATABASE}
            if names != expected:
                raise BackupError("Encrypted backup archive has an unexpected file layout")
            for info in archive.infolist():
                if info.is_dir() or Path(info.filename).name != info.filename:
                    raise BackupError("Encrypted backup archive contains an unsafe path")
            manifest = json.loads(archive.read(ARCHIVE_MANIFEST).decode("utf-8"))
            database_bytes = archive.read(ARCHIVE_DATABASE)
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("Encrypted backup archive is corrupt") from exc
    if not isinstance(manifest, dict):
        raise BackupError("Encrypted backup manifest is invalid")
    return manifest, database_bytes


def _encode_container(header: Mapping[str, Any], ciphertext: bytes) -> bytes:
    header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(header_bytes) > HEADER_LIMIT:
        raise BackupError("Backup header is unexpectedly large")
    return MAGIC + struct.pack(">I", len(header_bytes)) + header_bytes + ciphertext


def _decode_container(payload: bytes) -> tuple[dict[str, Any], bytes]:
    if not payload.startswith(MAGIC):
        raise BackupError("Not a Workout Agent encrypted backup")
    offset = len(MAGIC)
    if len(payload) < offset + 4:
        raise BackupError("Encrypted backup header is truncated")
    header_size = struct.unpack(">I", payload[offset : offset + 4])[0]
    if header_size <= 0 or header_size > HEADER_LIMIT:
        raise BackupError("Encrypted backup header length is invalid")
    header_start = offset + 4
    header_end = header_start + header_size
    if len(payload) <= header_end:
        raise BackupError("Encrypted backup payload is truncated")
    try:
        header = json.loads(payload[header_start:header_end].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("Encrypted backup header is invalid") from exc
    if not isinstance(header, dict):
        raise BackupError("Encrypted backup header is invalid")
    if header.get("format_version") != FORMAT_VERSION:
        raise BackupError("Unsupported encrypted backup format version")
    key_id = header.get("key_id")
    if not isinstance(key_id, str) or not KEY_ID_RE.fullmatch(key_id):
        raise BackupError("Encrypted backup key id is invalid")
    created_at = header.get("created_at")
    if not isinstance(created_at, str):
        raise BackupError("Encrypted backup timestamp is missing")
    _parse_utc(created_at)
    return header, payload[header_end:]


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def create_backup(
    source: Path,
    output: Path,
    keyring: Keyring,
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Create an authenticated encrypted backup from a consistent SQLite snapshot."""
    if output.exists():
        raise BackupError(f"Refusing to overwrite existing backup: {output}")
    timestamp = created_at or utc_now()
    _parse_utc(timestamp)
    with tempfile.TemporaryDirectory(prefix="workout-agent-backup-") as tmp_dir:
        snapshot = Path(tmp_dir) / "snapshot.sqlite3"
        _database_snapshot(source, snapshot)
        metadata = _database_metadata(snapshot)
        database_bytes = snapshot.read_bytes()
        manifest = {
            "format_version": FORMAT_VERSION,
            "created_at": timestamp,
            "key_id": keyring.active_key_id,
            "database_sha256": _sha256(database_bytes),
            "database_size_bytes": len(database_bytes),
            **metadata,
        }
        archive = _archive_bytes(database_bytes, manifest)
        ciphertext = Fernet(keyring.active_key).encrypt(archive)
        container = _encode_container(
            {
                "format_version": FORMAT_VERSION,
                "created_at": timestamp,
                "key_id": keyring.active_key_id,
            },
            ciphertext,
        )
        _atomic_write(output, container)
    return manifest


def _decrypt_backup(backup: Path, keyring: Keyring) -> tuple[dict[str, Any], bytes]:
    if not backup.is_file():
        raise BackupError(f"Encrypted backup does not exist: {backup}")
    header, ciphertext = _decode_container(backup.read_bytes())
    key_id = str(header["key_id"])
    key = keyring.keys.get(key_id)
    if key is None:
        raise BackupError(
            f"Backup uses key id {key_id!r}, which is not present in BACKUP_ENCRYPTION_KEYS"
        )
    try:
        plaintext = Fernet(key).decrypt(ciphertext)
    except InvalidToken as exc:
        raise BackupError("Encrypted backup authentication failed") from exc
    manifest, database_bytes = _extract_archive(plaintext)
    if manifest.get("format_version") != FORMAT_VERSION:
        raise BackupError("Encrypted manifest format version does not match")
    if manifest.get("key_id") != key_id:
        raise BackupError("Encrypted manifest key id does not match the outer envelope")
    if manifest.get("created_at") != header.get("created_at"):
        raise BackupError("Encrypted manifest timestamp does not match the outer envelope")
    if manifest.get("database_sha256") != _sha256(database_bytes):
        raise BackupError("Restored database digest does not match the encrypted manifest")
    if manifest.get("database_size_bytes") != len(database_bytes):
        raise BackupError("Restored database size does not match the encrypted manifest")
    return manifest, database_bytes


def verify_backup(backup: Path, keyring: Keyring) -> dict[str, Any]:
    """Decrypt into a disposable file and verify digest, counts and SQLite integrity."""
    manifest, database_bytes = _decrypt_backup(backup, keyring)
    with tempfile.TemporaryDirectory(prefix="workout-agent-verify-") as tmp_dir:
        candidate = Path(tmp_dir) / "candidate.sqlite3"
        candidate.write_bytes(database_bytes)
        actual = _database_metadata(candidate)
    for field in (
        "schema_sha256",
        "table_counts",
        "sqlite_user_version",
        "sqlite_application_id",
        "integrity_check",
        "foreign_key_violations",
    ):
        if manifest.get(field) != actual.get(field):
            raise BackupError(f"Restored database {field} does not match its manifest")
    return manifest


def restore_backup(backup: Path, output: Path, keyring: Keyring) -> dict[str, Any]:
    """Restore only to a new path so the operator can verify before an atomic cutover."""
    if output.exists():
        raise BackupError(
            f"Refusing to overwrite existing database: {output}; restore to a new path first"
        )
    manifest, database_bytes = _decrypt_backup(backup, keyring)
    with tempfile.TemporaryDirectory(prefix="workout-agent-restore-") as tmp_dir:
        candidate = Path(tmp_dir) / "candidate.sqlite3"
        candidate.write_bytes(database_bytes)
        actual = _database_metadata(candidate)
        for field in (
            "schema_sha256",
            "table_counts",
            "sqlite_user_version",
            "sqlite_application_id",
        ):
            if manifest.get(field) != actual.get(field):
                raise BackupError(f"Restore verification failed for {field}")
    _atomic_write(output, database_bytes)
    return manifest


def _outer_header(path: Path) -> dict[str, Any]:
    header, _ = _decode_container(path.read_bytes())
    return header


def retention_candidates(
    directory: Path,
    *,
    max_age_days: int,
    minimum_latest: int,
    now: datetime | None = None,
) -> tuple[list[Path], list[Path]]:
    if max_age_days < 1:
        raise BackupError("max_age_days must be at least 1")
    if minimum_latest < 1:
        raise BackupError("minimum_latest must be at least 1")
    now = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    threshold = now - timedelta(days=max_age_days)
    valid: list[tuple[datetime, Path]] = []
    skipped: list[Path] = []
    for path in sorted(directory.glob("*.wab")):
        try:
            created_at = _parse_utc(str(_outer_header(path)["created_at"]))
        except (BackupError, OSError):
            skipped.append(path)
            continue
        valid.append((created_at, path))
    valid.sort(key=lambda item: item[0], reverse=True)
    protected = {path for _, path in valid[:minimum_latest]}
    candidates = [
        path
        for created_at, path in valid
        if path not in protected and created_at < threshold
    ]
    return candidates, skipped


def _run_python(
    backend_dir: Path,
    code: str,
    *args: str,
    env_overrides: Mapping[str, str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(backend_dir)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=backend_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _init_current_schema(backend_dir: Path, database: Path) -> None:
    result = _run_python(
        backend_dir,
        "import sys; from database import init_db; init_db(sys.argv[1])",
        str(database),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [
            "unknown error"
        ]
        raise BackupError(f"Current schema migration failed: {detail[0]}")


def _application_startup_probe(
    repo_root: Path, backend_dir: Path, database: Path
) -> None:
    frontend_dist = repo_root / "frontend" / "dist" / "frontend" / "browser"
    created_dist = not frontend_dist.exists()
    frontend_dist.mkdir(parents=True, exist_ok=True)
    try:
        code = """
from starlette.testclient import TestClient
from webapp.secure_app import app
with TestClient(app) as client:
    response = client.get('/api/me')
    if response.status_code not in (200, 401):
        raise SystemExit(f'unexpected startup probe status: {response.status_code}')
"""
        result = _run_python(
            backend_dir,
            code,
            env_overrides={
                "DATABASE_PATH": str(database),
                "APP_ENV": "test",
                "ALLOW_ANONYMOUS_WEB": "1",
                "WEB_ALLOW_ANONYMOUS": "1",
            },
            timeout=45,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [
                "unknown error"
            ]
            raise BackupError(f"Application startup probe failed: {detail[0]}")
    finally:
        if created_dist:
            try:
                frontend_dist.rmdir()
                frontend_dist.parent.rmdir()
                frontend_dist.parent.parent.rmdir()
            except OSError:
                pass


def run_restore_drill(
    *,
    repo_root: Path,
    backend_dir: Path,
    backup: Path | None,
    keyring: Keyring | None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Restore a real backup or create a synthetic one, then migrate and boot it."""
    timestamp = occurred_at or utc_now()
    _parse_utc(timestamp)
    checks: list[dict[str, Any]] = []
    mode = "real-backup" if backup else "synthetic"
    with tempfile.TemporaryDirectory(prefix="workout-agent-restore-drill-") as tmp_dir:
        root = Path(tmp_dir)
        drill_backup = backup
        drill_keyring = keyring
        if backup is None:
            source = root / "source.sqlite3"
            _init_current_schema(backend_dir, source)
            with sqlite3.connect(source) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS backup_restore_probe ("
                    "id INTEGER PRIMARY KEY, tenant_ref TEXT NOT NULL, value TEXT NOT NULL)"
                )
                conn.executemany(
                    "INSERT INTO backup_restore_probe (tenant_ref, value) VALUES (?, ?)",
                    (
                        ("tenant-a", "alpha"),
                        ("tenant-a", "beta"),
                        ("tenant-b", "gamma"),
                    ),
                )
                conn.commit()
            synthetic_key = Fernet.generate_key()
            drill_keyring = Keyring({"drill": synthetic_key}, "drill")
            drill_backup = root / "synthetic.wab"
            create_backup(source, drill_backup, drill_keyring, created_at=timestamp)
            checks.append({"name": "encrypted-backup-create", "status": "pass"})
        if drill_backup is None or drill_keyring is None:
            raise BackupError("A keyring is required when drilling a real backup")

        verified = verify_backup(drill_backup, drill_keyring)
        checks.append({"name": "encrypted-backup-authentication", "status": "pass"})
        restored = root / "restored.sqlite3"
        restore_backup(drill_backup, restored, drill_keyring)
        before = _database_metadata(restored)
        checks.append({"name": "pre-migration-integrity", "status": "pass"})

        _init_current_schema(backend_dir, restored)
        after_migration = _database_metadata(restored)
        before_counts = before["table_counts"]
        after_counts = after_migration["table_counts"]
        regressions = {
            table: {"before": count, "after": after_counts.get(table)}
            for table, count in before_counts.items()
            if after_counts.get(table, -1) < count
        }
        if regressions:
            raise BackupError(
                f"Schema migration reduced record counts in {len(regressions)} table(s)"
            )
        checks.append(
            {
                "name": "schema-migration",
                "status": "pass",
                "schema_changed": (
                    before["schema_sha256"] != after_migration["schema_sha256"]
                ),
                "preexisting_table_count": len(before_counts),
                "post_migration_table_count": len(after_counts),
                "record_count_regressions": 0,
            }
        )
        checks.append({"name": "post-migration-integrity", "status": "pass"})

        _application_startup_probe(repo_root, backend_dir, restored)
        after_startup = _database_metadata(restored)
        if after_startup["table_counts"].get(
            "backup_restore_probe", 0
        ) < before_counts.get("backup_restore_probe", 0):
            raise BackupError("Application startup lost synthetic restore-drill records")
        checks.append({"name": "application-startup", "status": "pass"})

    evidence_seed = json.dumps(
        {
            "occurred_at": timestamp,
            "mode": mode,
            "backup_created_at": verified.get("created_at"),
            "backup_key_id": verified.get("key_id"),
            "checks": checks,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "schema_version": 1,
        "evidence_id": "backup-drill-" + _sha256(evidence_seed)[:16],
        "occurred_at": timestamp,
        "mode": mode,
        "status": "pass",
        "backup_created_at": verified.get("created_at"),
        "backup_key_id": verified.get("key_id"),
        "checks": checks,
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def command_create(args: argparse.Namespace) -> int:
    keyring = load_keyring(active_key_id=args.active_key_id)
    manifest = create_backup(Path(args.source), Path(args.output), keyring)
    print(
        f"Created encrypted backup {args.output} with key id {manifest['key_id']} "
        f"at {manifest['created_at']}"
    )
    return 0


def command_verify(args: argparse.Namespace) -> int:
    keyring = load_keyring()
    manifest = verify_backup(Path(args.backup), keyring)
    print(
        f"Verified encrypted backup {args.backup}: created {manifest['created_at']}, "
        f"key id {manifest['key_id']}, integrity ok"
    )
    return 0


def command_restore(args: argparse.Namespace) -> int:
    keyring = load_keyring()
    manifest = restore_backup(Path(args.backup), Path(args.output), keyring)
    print(
        f"Restored verified database to {args.output} from backup created "
        f"{manifest['created_at']}"
    )
    return 0


def command_prune(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    candidates, skipped = retention_candidates(
        directory,
        max_age_days=args.max_age_days,
        minimum_latest=args.minimum_latest,
    )
    for path in skipped:
        print(f"SKIP invalid/unreadable backup: {path}", file=sys.stderr)
    action = "DELETE" if args.apply else "WOULD DELETE"
    for path in candidates:
        print(f"{action} {path}")
        if args.apply:
            path.unlink()
    print(
        f"Retention policy selected {len(candidates)} backup(s); "
        f"preserving at least {args.minimum_latest} newest backup(s)."
    )
    return 0


def command_drill(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    backend_dir = (repo_root / args.backend_dir).resolve()
    backup = Path(args.backup).resolve() if args.backup else None
    keyring = load_keyring() if backup else None
    try:
        evidence = run_restore_drill(
            repo_root=repo_root,
            backend_dir=backend_dir,
            backup=backup,
            keyring=keyring,
            occurred_at=args.at,
        )
    except Exception as exc:  # noqa: BLE001
        evidence = {
            "schema_version": 1,
            "occurred_at": args.at or utc_now(),
            "mode": "real-backup" if backup else "synthetic",
            "status": "fail",
            "error": f"{type(exc).__name__}: {exc}",
        }
        _write_json(Path(args.output), evidence)
        print(f"Restore drill failed: {exc}", file=sys.stderr)
        return 1
    _write_json(Path(args.output), evidence)
    print(f"Restore drill passed: {evidence['evidence_id']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create an encrypted SQLite backup")
    create.add_argument("--source", required=True)
    create.add_argument("--output", required=True)
    create.add_argument("--active-key-id", default=None)
    create.set_defaults(func=command_create)

    verify = subparsers.add_parser(
        "verify", help="Decrypt and verify a backup without restoring it"
    )
    verify.add_argument("--backup", required=True)
    verify.set_defaults(func=command_verify)

    restore = subparsers.add_parser(
        "restore", help="Restore a verified backup to a new SQLite path"
    )
    restore.add_argument("--backup", required=True)
    restore.add_argument("--output", required=True)
    restore.set_defaults(func=command_restore)

    prune = subparsers.add_parser("prune", help="Apply encrypted-backup retention policy")
    prune.add_argument("--directory", required=True)
    prune.add_argument("--max-age-days", type=int, default=35)
    prune.add_argument("--minimum-latest", type=int, default=14)
    prune.add_argument("--apply", action="store_true")
    prune.set_defaults(func=command_prune)

    drill = subparsers.add_parser(
        "drill",
        help=(
            "Restore, migrate, integrity-check and start the application against a backup"
        ),
    )
    drill.add_argument("--repo-root", default=".")
    drill.add_argument("--backend-dir", default="backend")
    drill.add_argument(
        "--backup",
        default=None,
        help="Real encrypted backup; omitted for synthetic CI drill",
    )
    drill.add_argument("--output", default="artifacts/backup-restore-drill.json")
    drill.add_argument(
        "--at", default=None, help="ISO-8601 timestamp for deterministic evidence"
    )
    drill.set_defaults(func=command_drill)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except BackupError as exc:
        print(f"backup error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
