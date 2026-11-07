"""Helpers for building authenticated owner contexts in tests.

The repository layer only accepts an :class:`~ownership.AuthenticatedOwner`.
These helpers create the stored user first so the owner context can be minted
through the same factories production code uses.  Every value is synthetic.
"""

from __future__ import annotations

import base64
import json
import sqlite3

from database import get_or_create_user
from itsdangerous import TimestampSigner
from ownership import (
    _MINT_TOKEN,
    AuthenticatedOwner,
    owner_for_legacy_tenant,
    owner_for_user,
)


def create_owner(db_path: str, email: str) -> AuthenticatedOwner:
    """Return an authenticated owner context for a new or existing user."""

    record = get_or_create_user(email, email.split("@", 1)[0], db_path)
    return owner_for_user(db_path, record["id"])


def legacy_owner(db_path: str) -> AuthenticatedOwner:
    """Return the authenticated owner context for the stable legacy tenant."""

    return owner_for_legacy_tenant(db_path)


def synthetic_owner(user_id: str = "synthetic-owner") -> AuthenticatedOwner:
    """Return an owner context for unit tests that mock the database.

    Production code must mint owner contexts through the factories in
    :mod:`ownership`, which verify the identifier against the ``users`` table.
    """

    return AuthenticatedOwner(user_id, _MINT_TOKEN)


def signed_session_cookie(
    secret: str,
    user_id: str,
    email: str = "athlete@example.test",
) -> str:
    """Return a Starlette-compatible signed session cookie value."""

    payload = base64.b64encode(json.dumps({"user": email, "user_id": user_id}).encode())
    return TimestampSigner(secret).sign(payload).decode()


def insert_synthetic_row(
    conn: sqlite3.Connection,
    table: str,
    owner: object,
    tag: str,
    *,
    owner_column: str = "user_id",
) -> None:
    """Insert one valid synthetic row owned by *owner*, whatever the table is.

    The row is derived from ``PRAGMA table_info`` so the registry-driven
    ownership tests keep covering a table that is added later: only columns the
    database insists on are populated, and every text value carries *tag* so two
    tenants can hold rows with the same logical shape without colliding on a
    global key.  *owner* is written verbatim, which lets a test deliberately
    supply ``None`` or an unknown identifier.
    """

    columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
    primary_keys = [row for row in columns if row[5]]
    values: dict[str, object] = {}
    for _cid, name, declared, not_null, default, pk in columns:
        if name == owner_column:
            values[name] = owner
            continue
        kind = (declared or "").upper()
        is_rowid_alias = len(primary_keys) == 1 and pk and kind == "INTEGER"
        if is_rowid_alias:
            continue
        required = (bool(not_null) and default is None) or bool(pk)
        if not required:
            continue
        if "INT" in kind:
            values[name] = 1
        elif any(token in kind for token in ("REAL", "FLOA", "DOUB")):
            values[name] = 1.0
        else:
            values[name] = f"{tag}-{name}"

    names = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    conn.execute(
        f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
        tuple(values.values()),
    )


__all__ = [
    "create_owner",
    "insert_synthetic_row",
    "legacy_owner",
    "signed_session_cookie",
    "synthetic_owner",
]
