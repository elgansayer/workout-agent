"""Explicit, verified assignment of pre-account data to its owner.

Before accounts existed every row belonged to an implicit single user. Schema
upgrades keep that data under a reserved placeholder account
(``legacy@local``) that cannot sign in and is never scheduled. This module is
the only supported way to hand that data to a real person, and it is deliberately
a two-step, operator-driven process:

1. **Plan** (read-only). Names the intended owner by email and reports, per table,
   how many rows would move and how many would be left behind. The plan fails
   closed unless the owner is an *active* account that has authenticated through
   a provider-verified identity, so ownership can only be given to someone who
   demonstrably controls the account. It carries a digest of exactly what it
   found.
2. **Apply**. Re-derives the plan inside one write transaction, refuses if it
   differs from the digest the operator confirmed (stale or tampered), moves the
   rows, retires the placeholder and appends an immutable audit record.

Nothing is ever deleted or overwritten. When the owner already holds a row that
would collide with a legacy row (same key in a keyed table, or a second active
programme), the owner's row wins and the legacy row stays behind, reported in the
plan, on the retired placeholder. Data can be assigned to one owner only.

Restore the pre-assignment backup (see ``docs/BACKUP_RESTORE_RUNBOOK.md``) to
roll back; the audit record deliberately does not store row identifiers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import encryption
from accounts import AccountStatus, has_verified_identity
from database import (
    _USER_COLUMNS,
    DEFAULT_DB_PATH,
    _now_iso,
    _user_from_row,
    transaction,
)
from tenancy import LEGACY_TENANT_EMAIL, implicit_single_user_allowed, normalise_email

PLAN_VERSION = 1

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass(frozen=True)
class OwnedTable:
    """A user-owned table and how legacy rows merge into an existing owner.

    ``key`` lists the columns identifying a row within one tenant:

    * ``None`` - rows never collide (append-only history); all legacy rows move.
    * ``()`` - the tenant holds at most one row; it moves only if the owner has none.
    * ``("col", ...)`` - a legacy row moves only if the owner has no row with the
      same values in those columns.

    ``single_active`` additionally keeps a legacy *active* row behind when the
    owner already has an active one (a tenant may have a single active programme).
    """

    name: str
    key: tuple[str, ...] | None = None
    single_active: bool = False

    def __post_init__(self) -> None:
        # Names are interpolated into SQL, so they must be plain identifiers.
        # They only ever come from the constant registry below.
        for identifier in (self.name, *(self.key or ())):
            if not _IDENTIFIER.fullmatch(identifier):
                raise ValueError(f"unsafe SQL identifier: {identifier!r}")


#: Every table that stores per-user rows keyed by ``users.id``. A drift test
#: asserts this stays in sync with the schema.
OWNED_TABLES: tuple[OwnedTable, ...] = (
    OwnedTable("workout_history"),
    OwnedTable("exercise_progress"),
    OwnedTable("body_metrics"),
    OwnedTable("daily_log"),
    OwnedTable("check_ins"),
    OwnedTable("chat_messages"),
    OwnedTable("notifications"),
    OwnedTable("programme_state", ()),
    OwnedTable("hevy_meta", ("key",)),
    OwnedTable("hevy_routines", ("routine_key",)),
    OwnedTable("reasoning_logs", ("context_id",)),
    OwnedTable("dashboard_insights", ()),
    OwnedTable("deep_correlations", ()),
    OwnedTable("user_preferences", ()),
    OwnedTable("user_api_keys", ("provider",)),
    OwnedTable("push_subscriptions", ("endpoint",)),
    OwnedTable("programmes", ("template_key",), single_active=True),
    OwnedTable("programme_decisions"),
    OwnedTable("hevy_connections", ("provider",)),
    OwnedTable("training_rules", ()),
)


#: Tables that carry a ``user_id`` column but are deliberately *not* transferred,
#: with the reason. Anything else holding ``user_id`` that is in neither this
#: mapping nor :data:`OWNED_TABLES` blocks the assignment (``unmapped_user_table``)
#: rather than leaving its legacy rows stranded on a retired placeholder.
NOT_TRANSFERRED_TABLES: Mapping[str, str] = {
    "user_identities": (
        "identities belong to the person who signed in; the placeholder never "
        "has one"
    ),
    "csrf_tokens": "ephemeral, session-bound anti-forgery tokens that expire",
    "user_sessions": "ephemeral browser sessions never outlive the account",
    "credential_audit_events": "append-only security audit trail, not tenant data",
}


class OwnershipError(RuntimeError):
    """Base class for ownership-assignment failures."""


class OwnershipBlockedError(OwnershipError):
    """The plan has blockers, so nothing may be applied."""

    def __init__(self, blockers: tuple[str, ...]) -> None:
        super().__init__("Assignment is blocked: " + ", ".join(blockers))
        self.blockers = blockers


class OwnershipPlanMismatchError(OwnershipError):
    """The confirmed digest does not match what the database holds now."""


@dataclass(frozen=True)
class TablePlan:
    table: str
    movable: int
    left_behind: int


@dataclass(frozen=True)
class OwnershipPlan:
    """What assigning the legacy data to *owner_email* would do right now."""

    owner_email: str
    owner_user_id: str | None
    legacy_user_id: str | None
    tables: tuple[TablePlan, ...]
    blockers: tuple[str, ...]
    already_assigned: bool

    @property
    def movable_rows(self) -> int:
        return sum(entry.movable for entry in self.tables)

    @property
    def left_behind_rows(self) -> int:
        return sum(entry.left_behind for entry in self.tables)

    @property
    def can_apply(self) -> bool:
        return not self.blockers and self.movable_rows > 0

    @property
    def digest(self) -> str:
        """Fingerprint of the inputs and per-table outcome of this plan."""
        payload = {
            "version": PLAN_VERSION,
            "owner_email": self.owner_email,
            "owner_user_id": self.owner_user_id,
            "legacy_user_id": self.legacy_user_id,
            "tables": [
                {"table": t.table, "movable": t.movable, "left_behind": t.left_behind}
                for t in self.tables
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": PLAN_VERSION,
            "owner_email": self.owner_email,
            "owner_user_id": self.owner_user_id,
            "legacy_user_id": self.legacy_user_id,
            "already_assigned": self.already_assigned,
            "blockers": list(self.blockers),
            "movable_rows": self.movable_rows,
            "left_behind_rows": self.left_behind_rows,
            "tables": [
                {"table": t.table, "movable": t.movable, "left_behind": t.left_behind}
                for t in self.tables
            ],
            "digest": self.digest,
        }


@dataclass(frozen=True)
class OwnershipResult:
    #: ``assigned``, ``already_assigned`` or ``nothing_to_assign``.
    status: str
    moved: dict[str, int]
    left_behind: dict[str, int]
    plan_digest: str


def _movable_clause(table: OwnedTable) -> str:
    """SQL predicate (alias ``t``) selecting the legacy rows that may move."""
    clauses = ["t.user_id = :legacy"]
    if table.key is not None:
        match = "".join(f" AND o.{column} = t.{column}" for column in table.key)
        clauses.append(
            f"NOT EXISTS (SELECT 1 FROM {table.name} o "
            f"WHERE o.user_id = :owner{match})"
        )
    if table.single_active:
        clauses.append(
            f"NOT (t.active = 1 AND EXISTS (SELECT 1 FROM {table.name} o "
            "WHERE o.user_id = :owner AND o.active = 1))"
        )
    return " AND ".join(clauses)


def _table_plan(
    conn: sqlite3.Connection, table: OwnedTable, legacy_id: str, owner_id: str
) -> TablePlan:
    params = {"legacy": legacy_id, "owner": owner_id}
    movable = conn.execute(
        f"SELECT COUNT(*) FROM {table.name} t WHERE {_movable_clause(table)}",
        params,
    ).fetchone()[0]
    owned = conn.execute(
        f"SELECT COUNT(*) FROM {table.name} WHERE user_id = ?", (legacy_id,)
    ).fetchone()[0]
    return TablePlan(table.name, int(movable), int(owned) - int(movable))


def _unmapped_user_tables(conn: sqlite3.Connection) -> list[str]:
    """Return live tables that hold ``user_id`` but the plan does not know about."""
    known = {table.name for table in OWNED_TABLES} | set(NOT_TRANSFERRED_TABLES)
    rows = conn.execute(
        "SELECT m.name FROM sqlite_master AS m "
        "JOIN pragma_table_info(m.name) AS c "
        "WHERE m.type = 'table' AND m.name NOT LIKE 'sqlite_%' "
        "AND c.name = 'user_id' ORDER BY m.name"
    ).fetchall()
    return [str(row[0]) for row in rows if row[0] not in known]


def _build_plan(conn: sqlite3.Connection, owner_email: str) -> OwnershipPlan:
    email = normalise_email(owner_email)
    blockers: list[str] = []

    # A table the plan cannot move would be orphaned on the retired placeholder.
    blockers.extend(
        f"unmapped_user_table:{name}" for name in _unmapped_user_tables(conn)
    )

    legacy_row = conn.execute(
        "SELECT id FROM users WHERE email = ?", (LEGACY_TENANT_EMAIL,)
    ).fetchone()
    legacy_id = str(legacy_row[0]) if legacy_row else None

    owner_row = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users WHERE lower(email) = ?", (email,)
    ).fetchone()
    owner_id: str | None = None
    if owner_row is None:
        blockers.append("owner_not_found")
    else:
        owner = _user_from_row(owner_row)
        owner_id = owner["id"]
        if owner["email"] == LEGACY_TENANT_EMAIL:
            blockers.append("owner_is_legacy_placeholder")
        elif owner["status"] != AccountStatus.ACTIVE:
            blockers.append("owner_not_active")
        elif not has_verified_identity(conn, owner["id"]):
            blockers.append("owner_has_no_verified_identity")

    previous = conn.execute(
        "SELECT to_user_id FROM legacy_ownership_migrations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    already_assigned = previous is not None and previous[0] == owner_id
    if previous is not None and previous[0] != owner_id:
        blockers.append("already_assigned_to_different_owner")

    tables: tuple[TablePlan, ...] = ()
    if (
        legacy_id is not None
        and owner_id is not None
        and "owner_is_legacy_placeholder" not in blockers
    ):
        tables = tuple(
            _table_plan(conn, table, legacy_id, owner_id) for table in OWNED_TABLES
        )

    return OwnershipPlan(
        owner_email=email,
        owner_user_id=owner_id,
        legacy_user_id=legacy_id,
        tables=tables,
        blockers=tuple(blockers),
        already_assigned=already_assigned,
    )


def plan_legacy_ownership(
    db_path: str = DEFAULT_DB_PATH, *, owner_email: str
) -> OwnershipPlan:
    """Return what assigning the legacy data to *owner_email* would do.

    Read-only. Never raises for an ineligible owner; the reasons are listed in
    ``plan.blockers`` so an operator can see everything that needs fixing.
    """

    with transaction(db_path) as conn:
        return _build_plan(conn, owner_email)


def _transfer_credentials(
    conn: sqlite3.Connection,
    table: OwnedTable,
    entry: TablePlan,
    params: Mapping[str, str],
) -> int:
    """Move credential rows, re-sealing each value for the new owner.

    Vault ciphertext is bound to ``(user_id, provider, field)``, so a credential
    cannot be re-parented by updating ``user_id`` (the database enforces that
    immutability too). Each movable row is copied under the owner with its
    secret fields re-sealed, then the placeholder's row is deleted. Legacy
    Fernet/plaintext values are upgraded in the same step. A value that cannot
    be decrypted blocks the assignment instead of being carried over unreadable.
    """
    found = conn.execute(
        f"SELECT t.rowid AS _transfer_rowid, t.* FROM {table.name} t WHERE {_movable_clause(table)}",
        params,
    )
    columns = [description[0] for description in found.description]
    rows = [dict(zip(columns, row, strict=True)) for row in found.fetchall()]
    if len(rows) != entry.movable:
        raise OwnershipPlanMismatchError(
            f"{table.name} changed while the assignment was running."
        )
    if not rows:
        return 0
    try:
        master = encryption.load_master_key()
    except encryption.EncryptionUnavailableError as exc:
        raise OwnershipError(
            "Stored credentials cannot be re-sealed without ENCRYPTION_KEY."
        ) from exc

    for row in rows:
        provider = str(row["provider"])
        for field_name in ("api_key", "client_secret", "refresh_token"):
            value = row.get(field_name)
            if not value:
                continue
            try:
                if encryption.is_sealed(value):
                    plaintext = encryption.open_sealed(
                        master,
                        user_id=params["legacy"],
                        provider=provider,
                        field_name=field_name,
                        token=value,
                    )
                else:
                    plaintext = encryption.read_legacy_value(master, value)
                row[field_name] = encryption.seal(
                    master,
                    user_id=params["owner"],
                    provider=provider,
                    field_name=field_name,
                    plaintext=plaintext,
                )
            except encryption.DecryptionError as exc:
                raise OwnershipError(
                    "A stored credential could not be re-sealed for the new owner."
                ) from exc
        legacy_rowid = row.pop("_transfer_rowid")
        row.pop("id", None)
        row["user_id"] = params["owner"]
        conn.execute(
            f"INSERT INTO {table.name} ({', '.join(row)}) "
            f"VALUES ({', '.join(':' + name for name in row)})",
            row,
        )
        conn.execute(
            f"DELETE FROM {table.name} WHERE rowid = :rowid",
            {"rowid": legacy_rowid},
        )
    return len(rows)


def apply_legacy_ownership(
    db_path: str = DEFAULT_DB_PATH,
    *,
    owner_email: str,
    confirm_digest: str,
    applied_by: str,
    backup_verified: bool,
) -> OwnershipResult:
    """Assign the legacy data to *owner_email* exactly as the confirmed plan says.

    The operator must attest that a verified backup exists (``backup_verified``)
    and pass the digest from :func:`plan_legacy_ownership`. The whole assignment
    - every row move, the placeholder's retirement and the audit record - is one
    transaction, so a failure leaves the database untouched.

    Raises:
        OwnershipError: the backup attestation or digest is missing.
        OwnershipBlockedError: the owner is ineligible or data went to someone else.
        OwnershipPlanMismatchError: the data changed since the plan was made.
    """

    if backup_verified is not True:
        raise OwnershipError(
            "A verified backup must be confirmed before ownership is assigned."
        )
    if not applied_by.strip():
        raise OwnershipError("The operator performing the assignment is required.")

    with transaction(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        plan = _build_plan(conn, owner_email)
        if plan.blockers:
            raise OwnershipBlockedError(plan.blockers)
        left_behind = {t.table: t.left_behind for t in plan.tables if t.left_behind}
        if plan.movable_rows == 0:
            return OwnershipResult(
                status="already_assigned" if plan.already_assigned else "nothing_to_assign",
                moved={},
                left_behind=left_behind,
                plan_digest=plan.digest,
            )
        # Compare as bytes: ``compare_digest`` rejects non-ASCII ``str`` with a
        # TypeError, and a mistyped digest must be refused, not crash.
        confirmed = confirm_digest.strip().lower().encode("utf-8")
        if not hmac.compare_digest(plan.digest.encode("utf-8"), confirmed):
            raise OwnershipPlanMismatchError(
                "The database no longer matches the confirmed plan; generate a "
                "fresh plan and review it."
            )

        if plan.legacy_user_id is None or plan.owner_user_id is None:
            # Unreachable: rows can only be movable when both accounts exist.
            raise OwnershipError("The legacy tenant or the owner is missing.")
        params = {"legacy": plan.legacy_user_id, "owner": plan.owner_user_id}
        moved: dict[str, int] = {}
        for table, entry in zip(OWNED_TABLES, plan.tables, strict=True):
            if not entry.movable:
                continue
            if table.name == "user_api_keys":
                moved[table.name] = _transfer_credentials(conn, table, entry, params)
                continue
            cursor = conn.execute(
                f"UPDATE {table.name} SET user_id = :owner WHERE rowid IN ("
                f"SELECT t.rowid FROM {table.name} t WHERE {_movable_clause(table)})",
                params,
            )
            if cursor.rowcount != entry.movable:
                raise OwnershipPlanMismatchError(
                    f"{table.name} changed while the assignment was running."
                )
            moved[table.name] = cursor.rowcount

        now = _now_iso()
        conn.execute(
            "UPDATE users SET status = ?, status_changed_at = ?, updated_at = ? "
            "WHERE id = ? AND status IN (?, ?)",
            (
                AccountStatus.RETIRED.value,
                now,
                now,
                plan.legacy_user_id,
                AccountStatus.UNCLAIMED.value,
                AccountStatus.ACTIVE.value,
            ),
        )
        conn.execute(
            "INSERT INTO legacy_ownership_migrations "
            "(plan_digest, from_user_id, to_user_id, owner_email, summary_json, "
            "applied_by, applied_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                plan.digest,
                plan.legacy_user_id,
                plan.owner_user_id,
                plan.owner_email,
                json.dumps(
                    {
                        "plan_version": PLAN_VERSION,
                        "moved": moved,
                        "left_behind": left_behind,
                        "backup_verified": True,
                    },
                    sort_keys=True,
                ),
                applied_by.strip(),
                now,
            ),
        )
        return OwnershipResult(
            status="assigned",
            moved=moved,
            left_behind=left_behind,
            plan_digest=plan.digest,
        )


def get_deployment_owner_id(db_path: str = DEFAULT_DB_PATH) -> str | None:
    """Return the verified owner the legacy data was assigned to, if any."""

    with transaction(db_path) as conn:
        row = conn.execute(
            "SELECT to_user_id FROM legacy_ownership_migrations "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return str(row[0]) if row else None


def is_deployment_owner(user_id: str, db_path: str = DEFAULT_DB_PATH) -> bool:
    """Return whether *user_id* may use the deployment-wide connector settings.

    Server-level credentials (the single-user ``HEVY_API_KEY`` and Google Health
    settings) predate accounts. They belong to exactly one person: the verified
    owner the legacy data was assigned to. Before that assignment nobody holds
    them in production; in the explicit local mode they belong to the local
    tenant.
    """

    owner = get_deployment_owner_id(db_path)
    if owner is not None:
        return user_id == owner
    if not implicit_single_user_allowed():
        return False
    with transaction(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE email = ?", (LEGACY_TENANT_EMAIL,)
        ).fetchone()
    return row is not None and user_id == row[0]


__all__ = [
    "OWNED_TABLES",
    "OwnedTable",
    "OwnershipBlockedError",
    "OwnershipError",
    "OwnershipPlan",
    "OwnershipPlanMismatchError",
    "OwnershipResult",
    "TablePlan",
    "apply_legacy_ownership",
    "get_deployment_owner_id",
    "is_deployment_owner",
    "plan_legacy_ownership",
]
