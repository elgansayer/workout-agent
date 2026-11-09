#!/usr/bin/env python3
"""Adopt legacy single-user data into an explicit first production account.

Before the multi-user launch every row in the production database is either
unowned (``user_id IS NULL``) or owned by the synthetic ``legacy@local`` tenant
that ``database.init_db()`` creates. This one-time operator tool re-owns those
rows to one explicitly named production account so that they cannot be reached
through a legacy fallback (anonymous web mode, the scheduler's default user) and
are never visible to another account. See ``docs/LEGACY_DATA_MIGRATION.md``.

Safety contract
---------------
* ``--target-user-id`` is mandatory. The account must already exist and must
  not be the legacy tenant. Nothing is inferred.
* Ownership that is ambiguous is refused: nothing is changed and no backup is
  written. Ambiguous means rows owned by an account other than the legacy tenant
  and the target (including unknown owner ids), legacy rows that collide with
  rows the target already owns on a unique key, a schema that is not fully
  tenant scoped, or a database failing ``integrity_check``/``foreign_key_check``.
* Rows are only re-owned. Nothing is overwritten or deleted, so every table
  keeps its exact row count; this is verified before the commit. The single
  exception: when the target already has an active programme, adopted legacy
  programmes are stored inactive so the account keeps exactly one active
  programme (``get_active_programme`` relies on that). Transient session tables
  (``EPHEMERAL_TABLES``, i.e. ``csrf_tokens``) are left untouched.
* A verified encrypted ``.wab`` backup of the pre-migration state is mandatory.
  It is written while this process holds the database's only write lock and
  before the first row changes. Roll back by following
  ``docs/BACKUP_RESTORE_RUNBOOK.md`` (restore to a new path, then cut over).
* The whole adoption is one transaction; any failed check rolls it back.
* Idempotent: when nothing is left to adopt the run changes nothing and reports
  ``noop``. The two defaults that ``init_db()`` re-seeds for the legacy tenant on
  every start are left in place when the target already owns the same key.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # ``python tools/legacy_data_migration.py``
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import encryption

from tools.backup_restore import (
    BackupError,
    Keyring,
    create_backup,
    load_keyring,
    utc_now,
    verify_backup,
)

LEGACY_EMAIL = "legacy@local"
LOCK_TIMEOUT_SECONDS = 30.0

# Every table that carries ``user_id``. The schema is checked against this list
# at run time, so a new tenant-scoped table cannot be silently left behind.
USER_OWNED_TABLES: tuple[str, ...] = (
    "workout_history",
    "programme_state",
    "exercise_progress",
    "hevy_routines",
    "check_ins",
    "daily_log",
    "body_metrics",
    "chat_messages",
    "user_api_keys",
    "push_subscriptions",
    "notifications",
    "user_preferences",
    "programmes",
    "programme_revisions",
    "programme_routines",
    "programme_blocks",
    "exercise_prescriptions",
    "programme_week_states",
    "adaptation_decisions",
    "routine_workout_matches",
    "programme_decisions",
    "reasoning_logs",
    "dashboard_insights",
    "deep_correlations",
    "hevy_meta",
    "hevy_connections",
    "training_rules",
)

# Tables that carry ``user_id`` but hold transient session-security state, not
# user records. ``CSRFMiddleware`` creates ``csrf_tokens`` in the production
# database on first use. Its single-use rows expire within minutes and are bound
# to one signed session, so they are never re-owned, counted as ownership
# evidence or deleted. Every other unlisted ``user_id`` table still refuses.
# Authentication and audit tables are bound to real accounts (sessions,
# identities, credential audit trail) and never existed for the single-user
# deployment, so there is nothing to adopt; they are left untouched.
EPHEMERAL_TABLES: tuple[str, ...] = (
    "csrf_tokens",
    "user_sessions",
    "user_identities",
    "credential_audit_events",
)

# ``database.init_db()`` re-creates these two defaults for the legacy tenant on
# every start, so they reappear after a migration. When the target already owns
# the same key they are not data to adopt: they stay with the legacy tenant and
# are reported. Any other key collision is ambiguous and refuses the migration.
_RESEEDED_DEFAULTS: dict[str, str] = {
    "programme_state": "1 = 1",
    "hevy_meta": "a.\"key\" = 'programme_start_date'",
}


class MigrationError(RuntimeError):
    """Raised when the migration cannot proceed safely."""


@dataclass(frozen=True)
class TableCounts:
    total: int
    legacy: int  # unowned (NULL) or owned by the legacy tenant
    target: int
    other: int  # owned by any other (or an unknown) account

    def as_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "legacy": self.legacy,
            "target": self.target,
            "other": self.other,
        }


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _describe(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{table} ({count})" for table, count in sorted(counts.items()))


def _connect(db_path: Path) -> sqlite3.Connection:
    # Autocommit mode: the transaction below is managed explicitly.
    conn = sqlite3.connect(
        str(db_path), timeout=LOCK_TIMEOUT_SECONDS, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _tenant_tables(conn: sqlite3.Connection) -> set[str]:
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {
        name
        for name in names
        if name != "users"
        and any(
            column["name"] == "user_id"
            for column in conn.execute(f"PRAGMA table_info({_quote(name)})")
        )
    }


def _check_schema(conn: sqlite3.Connection) -> None:
    present = _tenant_tables(conn)
    missing = [table for table in USER_OWNED_TABLES if table not in present]
    uncovered = sorted(present - set(USER_OWNED_TABLES) - set(EPHEMERAL_TABLES))
    problems: list[str] = []
    if missing:
        problems.append(
            "tables without a user_id column (start the current release once so "
            f"init_db() upgrades the schema): {', '.join(missing)}"
        )
    if uncovered:
        problems.append(
            "tables with a user_id column that this tool does not adopt "
            f"(update USER_OWNED_TABLES): {', '.join(uncovered)}"
        )
    if problems:
        raise MigrationError("Schema is not safe to migrate: " + "; ".join(problems))


def _resolve_target(conn: sqlite3.Connection, target_user_id: str) -> sqlite3.Row:
    if not target_user_id.strip():
        raise MigrationError("An explicit target account id is required")
    row = conn.execute(
        "SELECT id, email FROM users WHERE id = ?", (target_user_id,)
    ).fetchone()
    if row is None:
        raise MigrationError(
            f"Target account {target_user_id!r} does not exist; the account must "
            "sign in once before its id can be used"
        )
    if str(row["email"]).strip().lower() == LEGACY_EMAIL:
        raise MigrationError(
            "Target account is the legacy tenant; choose the first real production account"
        )
    return row


def _legacy_user_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT id FROM users WHERE email = ?", (LEGACY_EMAIL,)
    ).fetchone()
    return None if row is None else str(row["id"])


def _scope_condition(
    legacy_id: str | None, alias: str = ""
) -> tuple[str, tuple[Any, ...]]:
    column = f"{alias}user_id"
    if legacy_id is None:
        return f"{column} IS NULL", ()
    return f"({column} IS NULL OR {column} = ?)", (legacy_id,)


def _counts(
    conn: sqlite3.Connection, legacy_id: str | None, target_user_id: str
) -> dict[str, TableCounts]:
    condition, params = _scope_condition(legacy_id)
    counts: dict[str, TableCounts] = {}
    for table in USER_OWNED_TABLES:
        name = _quote(table)
        total = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        legacy = conn.execute(
            f"SELECT COUNT(*) FROM {name} WHERE {condition}", params
        ).fetchone()[0]
        target = conn.execute(
            f"SELECT COUNT(*) FROM {name} WHERE user_id = ?", (target_user_id,)
        ).fetchone()[0]
        counts[table] = TableCounts(
            total=int(total),
            legacy=int(legacy),
            target=int(target),
            other=int(total) - int(legacy) - int(target),
        )
    return counts


def _unique_keys(conn: sqlite3.Connection, table: str) -> list[list[str]]:
    """Return the non-``user_id`` columns of every unique key that includes ``user_id``."""
    keys: list[list[str]] = []
    for index in conn.execute(f"PRAGMA index_list({_quote(table)})").fetchall():
        if not index["unique"]:
            continue
        # Partial unique indexes (e.g. one active programme per user) do not
        # describe a collision key for every row; the caller handles that
        # invariant explicitly.
        if index["partial"]:
            continue
        columns = [
            column["name"]
            for column in conn.execute(f"PRAGMA index_info({_quote(index['name'])})")
        ]
        if "user_id" in columns and None not in columns:
            keys.append([column for column in columns if column != "user_id"])
    return keys


def _colliding_rowids(
    conn: sqlite3.Connection,
    table: str,
    legacy_id: str | None,
    target_user_id: str,
    only: str = "1 = 1",
) -> set[int]:
    """Rowids of legacy rows that share a unique key with a row the target owns."""
    condition, params = _scope_condition(legacy_id, "a.")
    name = _quote(table)
    rowids: set[int] = set()
    for key_columns in _unique_keys(conn, table):
        same_key = (
            " AND ".join(f"b.{_quote(c)} = a.{_quote(c)}" for c in key_columns)
            or "1 = 1"
        )
        rows = conn.execute(
            f"SELECT a.rowid FROM {name} AS a WHERE {condition} AND ({only}) "
            f"AND EXISTS (SELECT 1 FROM {name} AS b WHERE b.user_id = ? AND {same_key})",
            (*params, target_user_id),
        )
        rowids.update(int(row[0]) for row in rows)
    return rowids


def _ownership_conflicts(
    conn: sqlite3.Connection, legacy_id: str | None, target_user_id: str
) -> tuple[dict[str, set[int]], dict[str, int]]:
    """Split key collisions into retained re-seeded defaults and real conflicts."""
    retained: dict[str, set[int]] = {}
    conflicts: dict[str, int] = {}
    for table in USER_OWNED_TABLES:
        colliding = _colliding_rowids(conn, table, legacy_id, target_user_id)
        if not colliding:
            continue
        defaults = (
            _colliding_rowids(
                conn, table, legacy_id, target_user_id, _RESEEDED_DEFAULTS[table]
            )
            if table in _RESEEDED_DEFAULTS
            else set()
        )
        if defaults:
            retained[table] = defaults
        if colliding - defaults:
            conflicts[table] = len(colliding - defaults)
    return retained, conflicts


def _assert_unambiguous(
    before: Mapping[str, TableCounts], conflicts: Mapping[str, int]
) -> None:
    problems: list[str] = []
    foreign = {table: count.other for table, count in before.items() if count.other}
    if foreign:
        problems.append(
            "rows owned by an account other than the legacy tenant and the target "
            f"(or by an unknown account): {_describe(foreign)}"
        )
    if conflicts:
        problems.append(
            "legacy rows that collide with rows the target already owns: "
            f"{_describe(conflicts)}"
        )
    if problems:
        raise MigrationError(
            "Ambiguous ownership; nothing was changed. " + "; ".join(problems)
        )


def _integrity(conn: sqlite3.Connection) -> dict[str, Any]:
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    return {
        "integrity_check": str(integrity),
        "foreign_key_violations": len(violations),
    }


def _require_healthy(
    conn: sqlite3.Connection, report: Mapping[str, Any], phase: str
) -> None:
    if report["integrity_check"] != "ok":
        raise MigrationError(
            f"SQLite integrity_check failed {phase} the migration: {report['integrity_check']}"
        )
    if report["foreign_key_violations"]:
        by_table: dict[str, int] = {}
        for row in conn.execute("PRAGMA foreign_key_check"):
            by_table[row[0]] = by_table.get(row[0], 0) + 1
        raise MigrationError(
            f"SQLite foreign_key_check reported violations {phase} the migration "
            f"(rows owned by an unknown account?): {_describe(by_table)}"
        )


_SEALED_FIELDS = ("api_key", "client_secret", "refresh_token")


def _reseal_credentials(
    conn: sqlite3.Connection,
    legacy_id: str | None,
    target_user_id: str,
    skipped: list[int],
) -> None:
    """Re-seal stored credentials for the target; ciphertext is bound to its owner.

    Plaintext only lives in memory and is never logged. A value that cannot be
    opened for its current owner (including any sealed value on an unowned row)
    refuses the migration, which rolls back.
    """
    condition, params = _scope_condition(legacy_id)
    exclusion = (
        f" AND rowid NOT IN ({', '.join('?' for _ in skipped)})" if skipped else ""
    )
    rows = conn.execute(
        f"SELECT rowid AS rid, user_id, provider, {', '.join(_SEALED_FIELDS)} "
        f"FROM user_api_keys WHERE {condition}{exclusion}",
        (*params, *skipped),
    ).fetchall()
    if not rows:
        return
    master = None
    prefix = f"{encryption.SEALED_PREFIX}."
    for row in rows:
        updates: dict[str, str] = {}
        for field in _SEALED_FIELDS:
            value = row[field]
            if not isinstance(value, str) or not value.startswith(prefix):
                continue
            if master is None:
                try:
                    master = encryption.load_master_key()
                except (encryption.EncryptionUnavailableError, ValueError) as exc:
                    raise MigrationError(
                        "Cannot re-seal stored credentials without a valid "
                        "ENCRYPTION_KEY"
                    ) from exc
            try:
                plain = encryption.open_sealed(
                    master,
                    user_id=row["user_id"] or "",
                    provider=row["provider"],
                    field_name=field,
                    token=value,
                )
            except encryption.DecryptionError as exc:
                raise MigrationError(
                    f"Cannot re-seal user_api_keys credential {row['rid']}: "
                    "it cannot be opened for its current owner"
                ) from exc
            updates[field] = encryption.seal(
                master,
                user_id=target_user_id,
                provider=row["provider"],
                field_name=field,
                plaintext=plain,
            )
        if updates:
            assignments = ", ".join(f"{field} = ?" for field in updates)
            conn.execute(
                f"UPDATE user_api_keys SET {assignments} WHERE rowid = ?",
                (*updates.values(), row["rid"]),
            )


def _reassign(
    conn: sqlite3.Connection,
    legacy_id: str | None,
    target_user_id: str,
    retained: Mapping[str, set[int]],
) -> dict[str, int]:
    condition, params = _scope_condition(legacy_id)
    moved: dict[str, int] = {}
    for table in USER_OWNED_TABLES:
        skipped = sorted(retained.get(table, ()))
        exclusion = (
            f" AND rowid NOT IN ({', '.join('?' for _ in skipped)})" if skipped else ""
        )
        if table == "user_api_keys":
            _reseal_credentials(conn, legacy_id, target_user_id, skipped)
        # The schema forbids re-parenting some rows (credentials). This audited
        # one-time adoption lifts that guard for its own transaction only; the
        # DDL rolls back with it and the guard is recreated before the commit.
        guards = [
            (name, sql)
            for name, sql in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = ? AND ("
                "name LIKE '%\\_user\\_immutable' ESCAPE '\\' OR "
                "name LIKE '%\\_no\\_update' ESCAPE '\\')",
                (table,),
            ).fetchall()
        ]
        for name, _sql in guards:
            conn.execute(f"DROP TRIGGER {_quote(name)}")
        try:
            cursor = conn.execute(
                f"UPDATE {_quote(table)} SET user_id = ? WHERE {condition}{exclusion}",
                (target_user_id, *params, *skipped),
            )
        except sqlite3.IntegrityError as exc:
            raise MigrationError(
                f"Ownership conflict while adopting {table}: {exc}"
            ) from exc
        for _name, sql in guards:
            conn.execute(sql)
        moved[table] = max(cursor.rowcount, 0)
    return moved


def _supersede_legacy_programmes(
    conn: sqlite3.Connection, legacy_id: str | None, target_user_id: str
) -> int:
    """Keep one active programme per account: the target's own choice wins."""
    if not conn.execute(
        "SELECT 1 FROM programmes WHERE user_id = ? AND active = 1", (target_user_id,)
    ).fetchone():
        return 0
    condition, params = _scope_condition(legacy_id)
    cursor = conn.execute(
        "UPDATE programmes SET active = 0, status = 'archived' "
        f"WHERE {condition} AND active = 1",
        params,
    )
    return max(cursor.rowcount, 0)


def _verify_after(
    before: Mapping[str, TableCounts],
    after: Mapping[str, TableCounts],
    moved: Mapping[str, int],
    retained: Mapping[str, set[int]],
) -> None:
    for table, was in before.items():
        kept = len(retained.get(table, ()))
        expected = TableCounts(
            total=was.total,
            legacy=kept,
            target=was.target + was.legacy - kept,
            other=was.other,
        )
        if after[table] != expected or moved[table] != was.legacy - kept:
            raise MigrationError(
                f"Post-migration verification failed for {table}: expected "
                f"{expected.as_dict()}, found {after[table].as_dict()}"
            )


def _write_verified_backup(
    db_path: Path,
    backup_output: Path,
    keyring: Keyring,
    before: Mapping[str, TableCounts],
) -> dict[str, Any]:
    manifest = create_backup(db_path, backup_output, keyring)
    try:
        verify_backup(backup_output, keyring)
        recorded = manifest["table_counts"]
        for table, counts in before.items():
            if recorded.get(table) != counts.total:
                raise MigrationError(
                    f"Backup row count for {table} does not match the database"
                )
    except (BackupError, MigrationError):
        # Never leave an unverified artefact that could be mistaken for a backup.
        with contextlib.suppress(OSError):
            backup_output.unlink()
        raise
    return manifest


def _adopt(
    conn: sqlite3.Connection,
    db_path: Path,
    target_user_id: str,
    backup_output: Path,
    keyring: Keyring,
) -> dict[str, Any]:
    """Validate, back up and re-own legacy rows inside the caller's transaction."""
    _check_schema(conn)
    _resolve_target(conn, target_user_id)
    legacy_id = _legacy_user_id(conn)
    integrity_before = _integrity(conn)
    _require_healthy(conn, integrity_before, "before")

    before = _counts(conn, legacy_id, target_user_id)
    retained, conflicts = _ownership_conflicts(conn, legacy_id, target_user_id)
    _assert_unambiguous(before, conflicts)

    adoptable = sum(
        count.legacy - len(retained.get(table, ())) for table, count in before.items()
    )
    manifest: dict[str, Any] | None = None
    moved = {table: 0 for table in before}
    superseded = 0
    after = before
    if adoptable:
        manifest = _write_verified_backup(db_path, backup_output, keyring, before)
        superseded = _supersede_legacy_programmes(conn, legacy_id, target_user_id)
        moved = _reassign(conn, legacy_id, target_user_id, retained)
        after = _counts(conn, legacy_id, target_user_id)
        _verify_after(before, after, moved, retained)
    integrity_after = _integrity(conn)
    _require_healthy(conn, integrity_after, "after")

    return {
        "schema_version": 1,
        "status": "migrated" if adoptable else "noop",
        "occurred_at": utc_now(),
        "database": str(db_path),
        "target_user_id": target_user_id,
        "legacy_user_id": legacy_id,
        "backup": None
        if manifest is None
        else {
            "path": str(backup_output),
            "created_at": manifest["created_at"],
            "key_id": manifest["key_id"],
            "database_sha256": manifest["database_sha256"],
        },
        "rows_reassigned": sum(moved.values()),
        "programmes_deactivated": superseded,
        # Tables where adopted rows joined records the target already owned.
        "merged_tables": sorted(
            table for table, count in before.items() if count.target and moved[table]
        ),
        "retained_default_rows": {
            table: len(rowids) for table, rowids in sorted(retained.items())
        },
        "integrity_before": integrity_before,
        "integrity_after": integrity_after,
        "before": {table: count.as_dict() for table, count in before.items()},
        "after": {table: count.as_dict() for table, count in after.items()},
    }


def migrate(
    db_path: Path,
    *,
    target_user_id: str,
    backup_output: Path,
    keyring: Keyring,
) -> dict[str, Any]:
    """Adopt legacy rows into ``target_user_id`` and return structured evidence."""
    if not db_path.is_file():
        raise MigrationError(f"Database does not exist: {db_path}")
    conn = _connect(db_path)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise MigrationError(
                f"Could not take the exclusive write lock; stop every writer: {exc}"
            ) from exc
        try:
            evidence = _adopt(conn, db_path, target_user_id, backup_output, keyring)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except sqlite3.Error as exc:
        raise MigrationError(f"SQLite error; no change was committed: {exc}") from exc
    finally:
        conn.close()
    return evidence


def _write_evidence(path: Path, evidence: Mapping[str, Any]) -> None:
    """Write evidence privately; never overwrite an earlier evidence file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(evidence, indent=2, sort_keys=True) + "\n")


def _print_summary(evidence: Mapping[str, Any]) -> None:
    print(f"Legacy data adoption: {evidence['status']}")
    print(f"Target account: {evidence['target_user_id']}")
    print(f"Legacy tenant: {evidence['legacy_user_id'] or 'none'}")
    backup = evidence["backup"]
    print(f"Pre-migration backup: {backup['path'] if backup else 'not needed (no-op)'}")
    print(f"Rows reassigned: {evidence['rows_reassigned']}")
    if evidence["programmes_deactivated"]:
        print(
            f"Legacy programmes stored inactive (target already has an active one): "
            f"{evidence['programmes_deactivated']}"
        )
    if evidence["merged_tables"]:
        print(
            "Merged into records the target already owned (not deduplicated): "
            + ", ".join(evidence["merged_tables"])
        )
    for table, count in evidence["retained_default_rows"].items():
        print(
            f"Retained re-seeded default left with the legacy tenant: {table} ({count})"
        )
    print("Per table (legacy rows before -> after, target rows before -> after):")
    for table in USER_OWNED_TABLES:
        was, now = evidence["before"][table], evidence["after"][table]
        print(
            f"  {table:20} legacy {was['legacy']:>6} -> {now['legacy']:>6}   "
            f"target {was['target']:>6} -> {now['target']:>6}   total {now['total']:>6}"
        )
    before, after = evidence["integrity_before"], evidence["integrity_after"]
    print(
        f"Integrity: {before['integrity_check']} -> {after['integrity_check']}; "
        f"foreign key violations {before['foreign_key_violations']} -> "
        f"{after['foreign_key_violations']}"
    )


def command_migrate(args: argparse.Namespace) -> int:
    evidence_output = Path(args.evidence) if args.evidence else None
    if evidence_output is not None and evidence_output.exists():
        print(
            f"migration error: refusing to overwrite evidence file {evidence_output}",
            file=sys.stderr,
        )
        return 1
    try:
        keyring = load_keyring()
    except BackupError as exc:
        print(f"migration error: {exc}", file=sys.stderr)
        return 2
    try:
        evidence = migrate(
            Path(args.database),
            target_user_id=args.target_user_id,
            backup_output=Path(args.backup_output),
            keyring=keyring,
        )
    except (MigrationError, BackupError) as exc:
        print(f"migration error: {exc}", file=sys.stderr)
        return 1
    _print_summary(evidence)
    if evidence_output is not None:
        try:
            _write_evidence(evidence_output, evidence)
        except OSError as exc:
            print(
                "migration error: the migration committed but its evidence could not "
                f"be written: {exc}",
                file=sys.stderr,
            )
            return 1
        print(f"Evidence: {evidence_output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    adopt = subparsers.add_parser(
        "migrate",
        help="Adopt legacy/unowned rows into an explicit first production account",
    )
    adopt.add_argument("--database", required=True, help="Path to the SQLite database")
    adopt.add_argument(
        "--target-user-id",
        required=True,
        help="Existing account id that will own the legacy data",
    )
    adopt.add_argument(
        "--backup-output",
        required=True,
        help="New path for the mandatory pre-migration encrypted .wab backup",
    )
    adopt.add_argument(
        "--evidence",
        default=None,
        help="Optional new path for JSON before/after evidence (mode 0600)",
    )
    adopt.set_defaults(func=command_migrate)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
