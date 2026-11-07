"""Authenticated tenant ownership context and database ownership constraints.

Every user-owned record in Workout Agent is identified by an immutable owner
column that references the ``users`` table.  Application-level filters alone
cannot guarantee isolation, so this module provides complementary enforcement
points:

* :class:`AuthenticatedOwner` is the only tenant context accepted by the
  repository and service layers.  It cannot be constructed from a plain
  identifier: it is minted from a verified signed session or from a stored user
  row, so a caller cannot pass an arbitrary identifier to reach another
  account.  Repositories convert the context into an identifier through
  :func:`owner_id`, which re-checks the type at runtime so a duck-typed object,
  a mock or a raw string fails closed instead of being trusted.
* The :data:`USER_SCOPED_TABLES` registry is the single source of truth for
  the database-level contract.  :func:`audit_ownership_constraints` verifies
  that every registered table has a non-null owner column, a foreign key to the
  owner table with the declared delete policy, an indexed access path that
  leads with the owner column, and a trigger that makes the owner immutable.
  :func:`enforce_ownership_constraints` fails closed when the contract is not
  met, and :func:`find_unregistered_owner_tables` reports tables that carry an
  owner column without being registered.

The canonical owner table DDL lives here so every connection that needs to
reference ``users(id)`` — including the CSRF token store — creates exactly the
same schema.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Final, final

OWNER_TABLE: Final[str] = "users"
OWNER_COLUMN: Final[str] = "user_id"
LEGACY_TENANT_EMAIL: Final[str] = "legacy@local"

#: Foreign-key delete policy for domain data.  Deleting a user that still owns
#: records fails closed until a deliberate, auditable deletion workflow removes
#: those records; no statement can silently erase a tenant by deleting one row.
RESTRICT_ON_DELETE: Final[str] = "NO ACTION"
#: Delete policy for ephemeral security nonces that are meaningless without
#: their owner.
CASCADE_ON_DELETE: Final[str] = "CASCADE"

USERS_TABLE_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    display_name TEXT,
    created_at TEXT NOT NULL,
    timezone TEXT DEFAULT 'UTC',
    units TEXT DEFAULT 'metric'
)
"""

CSRF_TOKENS_TABLE_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS csrf_tokens (
    token_hash TEXT PRIMARY KEY,
    session_hash TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL
)
"""

CSRF_TOKENS_INDEX_SQL: Final[str] = (
    "CREATE INDEX IF NOT EXISTS idx_csrf_tokens_user_expiry "
    "ON csrf_tokens (user_id, expires_at)"
)


class OwnershipError(RuntimeError):
    """Base class for tenant ownership failures."""


class UnauthenticatedOwnerError(OwnershipError):
    """Raised when an owner context is requested without a verified identity."""


class UnknownOwnerError(OwnershipError):
    """Raised when an owner identifier does not resolve to a stored user."""


class InvalidOwnerContextError(OwnershipError, TypeError):
    """Raised when a caller supplies something other than an authenticated owner."""


class OwnershipConstraintError(OwnershipError):
    """Raised when a user-scoped table violates its ownership contract."""


_MINT_TOKEN: Final[object] = object()


@final
class AuthenticatedOwner:
    """An immutable, verified tenant context.

    The class is deliberately not constructible with a plain identifier: the
    private mint token means only the factories in this module can create one.
    It is a plain slotted class rather than a dataclass so that helpers such as
    :func:`dataclasses.replace` cannot copy the mint token onto a different
    identifier, and it refuses subclassing, copying with a new identity and
    serialisation so a verified context cannot be persisted in a shared cache
    and revived for another request.
    """

    __slots__ = ("_user_id",)

    _user_id: str

    def __init__(self, user_id: str, _mint: object = None) -> None:
        if _mint is not _MINT_TOKEN:
            raise TypeError(
                "AuthenticatedOwner must be created through ownership.py factories"
            )
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("An authenticated owner requires a non-empty user id")
        object.__setattr__(self, "_user_id", user_id)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("AuthenticatedOwner cannot be subclassed")

    @property
    def user_id(self) -> str:
        """The verified identifier of the owning user."""

        return self._user_id

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("AuthenticatedOwner is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("AuthenticatedOwner is immutable")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AuthenticatedOwner):
            return NotImplemented
        return self._user_id == other._user_id

    def __hash__(self) -> int:
        return hash((AuthenticatedOwner, self._user_id))

    def __repr__(self) -> str:
        return f"AuthenticatedOwner(user_id={self._user_id!r})"

    def __copy__(self) -> AuthenticatedOwner:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> AuthenticatedOwner:
        return self

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError(
            "AuthenticatedOwner cannot be serialised; mint a new context from the "
            "stored user instead"
        )


def owner_id(owner: AuthenticatedOwner) -> str:
    """Return the verified identifier carried by an authenticated owner.

    This is the only place a repository turns an owner context into a SQL
    parameter.  The runtime type check means a raw string, a ``None``, a mock or
    any look-alike object with a ``user_id`` attribute is rejected even when
    static typing was bypassed.
    """

    if type(owner) is not AuthenticatedOwner:
        raise InvalidOwnerContextError(
            "An authenticated owner context is required; got "
            f"{type(owner).__name__}"
        )
    return owner.user_id


def _session_has_verified_user(session: Mapping[str, Any]) -> bool:
    """Return whether the session carries a complete identity."""

    user = session.get("user")
    if isinstance(user, str):
        return bool(user.strip())
    if isinstance(user, Mapping):
        return bool(user.get("email") or user.get("id"))
    return False


def owner_from_session(session: Mapping[str, Any] | None) -> AuthenticatedOwner:
    """Mint an owner context from a verified signed session.

    Raises :class:`UnauthenticatedOwnerError` when the session is missing or
    does not carry both identity components.
    """

    if not isinstance(session, Mapping):
        raise UnauthenticatedOwnerError("A verified session is required")
    user_id = session.get("user_id")
    if not isinstance(user_id, str) or not user_id.strip():
        raise UnauthenticatedOwnerError("The session does not contain a user id")
    if not _session_has_verified_user(session):
        raise UnauthenticatedOwnerError("The session does not contain a verified user")
    return AuthenticatedOwner(user_id.strip(), _MINT_TOKEN)


def owner_for_user(db_path: str, user_id: str) -> AuthenticatedOwner:
    """Mint an owner context for a stored user.

    Background jobs and services use this factory.  The identifier must
    resolve to a row in ``users``; unknown identifiers are rejected so a
    caller cannot persist records for a tenant that does not exist.
    """

    if not isinstance(user_id, str) or not user_id.strip():
        raise UnknownOwnerError("An owner identifier is required")
    normalised = user_id.strip()
    try:
        with closing(sqlite3.connect(db_path, timeout=10)) as conn:
            row = conn.execute(
                f"SELECT id FROM {OWNER_TABLE} WHERE id = ?",
                (normalised,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise UnknownOwnerError(f"Could not verify the owner identifier: {exc}") from exc
    if row is None:
        raise UnknownOwnerError("The owner identifier does not resolve to a stored user")
    return AuthenticatedOwner(normalised, _MINT_TOKEN)


def owner_for_email(db_path: str, email: str) -> AuthenticatedOwner:
    """Mint an owner context for a stored user identified by email."""

    if not isinstance(email, str) or not email.strip():
        raise UnknownOwnerError("An owner email is required")
    try:
        with closing(sqlite3.connect(db_path, timeout=10)) as conn:
            row = conn.execute(
                f"SELECT id FROM {OWNER_TABLE} WHERE email = ?",
                (email.strip(),),
            ).fetchone()
    except sqlite3.Error as exc:
        raise UnknownOwnerError(f"Could not verify the owner email: {exc}") from exc
    if row is None:
        raise UnknownOwnerError("The owner email does not resolve to a stored user")
    return AuthenticatedOwner(str(row[0]), _MINT_TOKEN)


def owner_for_legacy_tenant(db_path: str) -> AuthenticatedOwner:
    """Mint the owner context for the stable legacy tenant.

    Single-tenant deployments and the deliberate read-only anonymous mode use
    this tenant.  It still resolves through ``users`` so the database-level
    foreign key applies exactly as it does for every other tenant.
    """

    return owner_for_email(db_path, LEGACY_TENANT_EMAIL)


@dataclass(frozen=True, slots=True)
class UserScopedTable:
    """The database-level ownership contract for a user-owned table."""

    name: str
    owner_column: str = OWNER_COLUMN
    require_foreign_key: bool = True
    require_not_null: bool = True
    require_owner_index: bool = True
    require_immutable_owner: bool = True
    on_delete: str = RESTRICT_ON_DELETE
    reference: str = ""

    @property
    def owner_index_prefix(self) -> str:
        return f"idx_{self.name}_{self.owner_column}"

    @property
    def immutability_trigger(self) -> str:
        return f"trg_{self.name}_{self.owner_column}_immutable"


def _core_table(name: str, reference: str) -> UserScopedTable:
    return UserScopedTable(name=name, reference=reference)


#: Every table that stores user-owned records.  The registry drives the
#: migration, the startup audit and the IDOR matrix so the contract cannot
#: drift between the schema, the repository and the tests.
USER_SCOPED_TABLES: Final[tuple[UserScopedTable, ...]] = (
    _core_table("workout_history", "raw Hevy payloads"),
    _core_table("programme_state", "current cycle position"),
    _core_table("exercise_progress", "per-exercise top sets"),
    _core_table("hevy_routines", "synced Hevy routine ids"),
    _core_table("hevy_meta", "per-user metadata"),
    _core_table("check_ins", "programme check-ins"),
    _core_table("daily_log", "issued daily plans"),
    _core_table("body_metrics", "body composition readings"),
    _core_table("dashboard_insights", "dashboard insight snapshots"),
    _core_table("deep_correlations", "weekly correlation narratives"),
    _core_table("reasoning_logs", "exercise reasoning records"),
    _core_table("chat_messages", "coach conversation history"),
    _core_table("user_api_keys", "encrypted provider credentials"),
    _core_table("user_preferences", "training preferences"),
    _core_table("programmes", "activated programme definitions"),
    _core_table("push_subscriptions", "web push subscriptions"),
    _core_table("notifications", "in-app notifications"),
    UserScopedTable(
        name="csrf_tokens",
        on_delete=CASCADE_ON_DELETE,
        reference="single-use CSRF nonces",
    ),
    # The canonical health platform owns records with an integer identity that
    # is not yet unified with ``users.id``.  The foreign key is deferred to the
    # health-platform identity migration; the non-null owner column, the
    # positive-owner check, the indexed access path and the immutable owner are
    # enforced now.
    UserScopedTable(
        name="health_connections",
        require_foreign_key=False,
        reference="staged health platform owner",
    ),
    UserScopedTable(
        name="health_metrics",
        require_foreign_key=False,
        reference="staged health platform owner",
    ),
    UserScopedTable(
        name="health_sync_runs",
        require_foreign_key=False,
        reference="staged health platform owner",
    ),
)

USER_SCOPED_TABLE_NAMES: Final[frozenset[str]] = frozenset(
    table.name for table in USER_SCOPED_TABLES
)


def registered_table(name: str) -> UserScopedTable:
    """Return the registry entry for *name* or fail closed."""

    for table in USER_SCOPED_TABLES:
        if table.name == name:
            return table
    raise OwnershipConstraintError(f"{name} is not a registered user-scoped table")

#: Tables that intentionally have no tenant owner.  ``users`` is the owner
#: table itself; ``rate_limits`` is keyed by client address, not by account.
OWNERLESS_TABLE_NAMES: Final[frozenset[str]] = frozenset({OWNER_TABLE, "rate_limits"})


@dataclass(frozen=True, slots=True)
class OwnershipViolation:
    """A single breach of a table's ownership contract."""

    table: str
    detail: str

    def __str__(self) -> str:
        return f"{self.table}: {self.detail}"


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _identifier(value: str) -> str:
    """Return *value* when it is a plain SQL identifier, else fail closed.

    Table and column names are interpolated into DDL, so they must never carry
    anything but an identifier.
    """

    if not _IDENTIFIER.fullmatch(value):
        raise OwnershipConstraintError(f"Unsafe SQL identifier: {value!r}")
    return value


def owner_immutability_trigger_sql(table: UserScopedTable) -> str:
    """Return the DDL for the trigger that forbids reassigning an owner.

    The trigger fires only when the owner column is assigned a different value,
    so upserts that rewrite other columns, and those that restate the same
    owner, are unaffected.
    """

    name = _identifier(table.immutability_trigger)
    target = _identifier(table.name)
    column = _identifier(table.owner_column)
    return (
        f"CREATE TRIGGER IF NOT EXISTS {name} "
        f"BEFORE UPDATE OF {column} ON {target} "
        f"FOR EACH ROW WHEN NEW.{column} IS NOT OLD.{column} "
        f"BEGIN SELECT RAISE(ABORT, 'owner is immutable: {target}.{column}'); END"
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> dict[str, tuple[Any, ...]]:
    return {
        str(row[1]): tuple(row)
        for row in conn.execute(f"PRAGMA table_info({_identifier(table)})").fetchall()
    }


def _column_is_not_null(conn: sqlite3.Connection, table: str, column: str) -> bool:
    row = _table_columns(conn, table).get(column)
    return bool(row[3]) if row is not None else False


def _owner_foreign_key_actions(
    conn: sqlite3.Connection,
    table: str,
    column: str,
) -> list[str]:
    """Return the ``ON DELETE`` actions of foreign keys from *column* to users."""

    rows = conn.execute(f"PRAGMA foreign_key_list({_identifier(table)})").fetchall()
    return [
        str(row[6]).upper()
        for row in rows
        if row[2] == OWNER_TABLE and row[3] == column and row[4] == "id"
    ]


def _has_owner_index(conn: sqlite3.Connection, table: str, column: str) -> bool:
    index_names = conn.execute(
        "SELECT name FROM pragma_index_list(?)",
        (table,),
    ).fetchall()
    for (index_name,) in index_names:
        columns = conn.execute(
            "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
            (index_name,),
        ).fetchall()
        if columns and columns[0][0] == column:
            return True
    return False


def _normalised_sql(sql: str) -> str:
    return " ".join(sql.lower().split())


def _has_owner_immutability_trigger(
    conn: sqlite3.Connection,
    table: UserScopedTable,
) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'trigger' AND name = ? AND tbl_name = ?",
        (table.immutability_trigger, table.name),
    ).fetchone()
    if row is None or not row[0]:
        return False
    sql = _normalised_sql(str(row[0]))
    return (
        f"before update of {table.owner_column.lower()} on {table.name.lower()}" in sql
        and f"new.{table.owner_column.lower()} is not old.{table.owner_column.lower()}"
        in sql
        and "raise(abort" in sql
    )


def audit_ownership_constraints(
    conn: sqlite3.Connection,
    *,
    tables: tuple[UserScopedTable, ...] = USER_SCOPED_TABLES,
) -> tuple[OwnershipViolation, ...]:
    """Return every breach of the registered ownership contract.

    Tables that do not exist yet are ignored: the audit describes the tables
    that are present, so a staged foundation is not reported as missing.
    """

    violations: list[OwnershipViolation] = []
    for table in tables:
        if not _table_exists(conn, table.name):
            continue
        columns = _table_columns(conn, table.name)
        if table.owner_column not in columns:
            violations.append(
                OwnershipViolation(table.name, f"missing owner column {table.owner_column}")
            )
            continue
        if table.require_not_null and not _column_is_not_null(
            conn, table.name, table.owner_column
        ):
            violations.append(
                OwnershipViolation(
                    table.name, f"owner column {table.owner_column} allows null owners"
                )
            )
        if table.require_foreign_key:
            actions = _owner_foreign_key_actions(conn, table.name, table.owner_column)
            if not actions:
                violations.append(
                    OwnershipViolation(
                        table.name,
                        f"owner column {table.owner_column} does not reference "
                        f"{OWNER_TABLE}(id)",
                    )
                )
            elif table.on_delete.upper() not in actions:
                violations.append(
                    OwnershipViolation(
                        table.name,
                        f"owner foreign key uses ON DELETE {actions[0]} but the "
                        f"contract requires ON DELETE {table.on_delete.upper()}",
                    )
                )
        if table.require_owner_index and not _has_owner_index(
            conn, table.name, table.owner_column
        ):
            violations.append(
                OwnershipViolation(
                    table.name,
                    f"no index leads with {table.owner_column}",
                )
            )
        if table.require_immutable_owner and not _has_owner_immutability_trigger(
            conn, table
        ):
            violations.append(
                OwnershipViolation(
                    table.name,
                    f"owner column {table.owner_column} is not protected by "
                    f"trigger {table.immutability_trigger}",
                )
            )
    return tuple(violations)


def enforce_ownership_constraints(
    conn: sqlite3.Connection,
    *,
    tables: tuple[UserScopedTable, ...] = USER_SCOPED_TABLES,
) -> None:
    """Raise :class:`OwnershipConstraintError` when the contract is broken."""

    violations = audit_ownership_constraints(conn, tables=tables)
    if violations:
        details = "; ".join(str(violation) for violation in violations)
        raise OwnershipConstraintError(
            f"User-scoped tables violate the ownership contract: {details}"
        )


def install_owner_immutability_triggers(
    conn: sqlite3.Connection,
    *,
    tables: Iterable[UserScopedTable] = USER_SCOPED_TABLES,
) -> None:
    """Create the owner-immutability trigger on every present registered table.

    The statement is idempotent, so it is safe to run on every startup and
    after a table rebuild (which drops the old table's triggers with it).
    """

    for table in tables:
        if not table.require_immutable_owner or not _table_exists(conn, table.name):
            continue
        if table.owner_column not in _table_columns(conn, table.name):
            continue
        conn.execute(owner_immutability_trigger_sql(table))


def find_unregistered_owner_tables(
    conn: sqlite3.Connection,
    *,
    registered: frozenset[str] = USER_SCOPED_TABLE_NAMES,
    ownerless: frozenset[str] = OWNERLESS_TABLE_NAMES,
) -> tuple[str, ...]:
    """Return tables that carry an owner column without being registered.

    A table that stores per-user rows but is missing from
    :data:`USER_SCOPED_TABLES` would silently escape the ownership contract, the
    startup audit and the IDOR matrix.  The test suite fails when this list is
    not empty.
    """

    names = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    return tuple(
        name
        for name in names
        if name not in registered
        and name not in ownerless
        and OWNER_COLUMN in _table_columns(conn, name)
    )


__all__ = [
    "CASCADE_ON_DELETE",
    "CSRF_TOKENS_INDEX_SQL",
    "CSRF_TOKENS_TABLE_SQL",
    "LEGACY_TENANT_EMAIL",
    "OWNERLESS_TABLE_NAMES",
    "OWNER_COLUMN",
    "OWNER_TABLE",
    "RESTRICT_ON_DELETE",
    "USERS_TABLE_SQL",
    "USER_SCOPED_TABLES",
    "USER_SCOPED_TABLE_NAMES",
    "AuthenticatedOwner",
    "InvalidOwnerContextError",
    "OwnershipConstraintError",
    "OwnershipError",
    "OwnershipViolation",
    "UnauthenticatedOwnerError",
    "UnknownOwnerError",
    "UserScopedTable",
    "audit_ownership_constraints",
    "enforce_ownership_constraints",
    "find_unregistered_owner_tables",
    "install_owner_immutability_triggers",
    "owner_for_email",
    "owner_for_legacy_tenant",
    "owner_for_user",
    "owner_from_session",
    "owner_id",
    "owner_immutability_trigger_sql",
    "registered_table",
]
