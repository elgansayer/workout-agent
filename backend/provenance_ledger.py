"""Append-only, tenant-scoped SQLite ledger of record provenance and lineage.

Each row is one *revision* of one logical record, identified by
``(user_id, record_type, record_key)``. A revision stores the provenance fields
the product promises (source, external identifier, source timestamp, ingestion
time, transformation version, confidence) and, for corrections, a link to the
revision it superseded. Rows are never updated, so a correction or re-import
adds history instead of overwriting it.

Every function takes an open connection so a writer can record provenance in
the same transaction as the record itself, and every query filters on
``user_id``; callers must pass the authenticated owner, never a client claim.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from provenance import (
    ALLOWED_KINDS,
    LEGACY_TRANSFORMATION_VERSION,
    Provenance,
    RecordKind,
    RecordType,
    api_view,
    clean_note,
    content_hash,
    source_label,
    unattributed,
)

logger = logging.getLogger(__name__)

HELD_NOTE = "Not applied: a user-entered value takes precedence over this import."
BASELINE_NOTE = (
    "Captured from a record stored before provenance tracking; its origin was not recorded."
)

_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_.:@+-]{1,200}$")
_IN_CHUNK = 400

LEDGER_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS record_provenance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL REFERENCES users(id),
        record_type TEXT NOT NULL,
        record_key TEXT NOT NULL,
        revision INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('imported', 'user_entered', 'derived')),
        source TEXT NOT NULL,
        external_id TEXT,
        source_timestamp TEXT,
        ingested_at TEXT NOT NULL,
        transformation_version TEXT NOT NULL,
        confidence REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
        contract_version INTEGER NOT NULL DEFAULT 1,
        applied INTEGER NOT NULL DEFAULT 1 CHECK (applied IN (0, 1)),
        supersedes_id INTEGER REFERENCES record_provenance(id),
        held_by_id INTEGER REFERENCES record_provenance(id),
        lineage_note TEXT,
        content_hash TEXT NOT NULL,
        retained_content TEXT,
        UNIQUE (user_id, record_type, record_key, revision)
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS idx_record_provenance_user_type_time "
        "ON record_provenance (user_id, record_type, ingested_at DESC)"
    ),
    # Lineage must not be rewritten: corrections append a revision instead.
    """
    CREATE TRIGGER IF NOT EXISTS trg_record_provenance_append_only
    BEFORE UPDATE ON record_provenance
    BEGIN
        SELECT RAISE(ABORT, 'record_provenance is append-only');
    END
    """,
)

_COLUMNS = (
    "p.id, p.record_type, p.record_key, p.revision, p.applied, p.supersedes_id, "
    "prev.revision, p.held_by_id, p.lineage_note, p.content_hash, p.retained_content, "
    "p.kind, p.source, p.external_id, p.source_timestamp, p.ingested_at, "
    "p.transformation_version, p.confidence"
)
_FROM = (
    "FROM record_provenance p "
    "LEFT JOIN record_provenance prev "
    "ON prev.id = p.supersedes_id AND prev.user_id = p.user_id"
)


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One stored revision, with its provenance and optional retained value."""

    id: int
    record_type: str
    record_key: str
    revision: int
    applied: bool
    supersedes_id: int | None
    supersedes_revision: int | None
    held_by_id: int | None
    note: str | None
    content_hash: str
    content: Any
    provenance: Provenance

    @property
    def corrected(self) -> bool:
        return (
            self.provenance.kind is RecordKind.USER_ENTERED
            and self.supersedes_id is not None
        )

    def view(self) -> dict[str, Any]:
        view = api_view(
            self.provenance,
            revision=self.revision,
            corrected=self.corrected,
            applied=self.applied,
            supersedes_revision=self.supersedes_revision,
            note=self.note,
        )
        if self.content is not None:
            view["content"] = self.content
        return view


@dataclass(frozen=True, slots=True)
class RevisionOutcome:
    """Result of ``record_revision``: recorded, unchanged, or held back."""

    status: str
    entry_id: int
    revision: int

    @property
    def applied(self) -> bool:
        return self.status != "held"


def ensure_schema(cursor: sqlite3.Cursor) -> None:
    """Create the ledger table, index and append-only trigger (idempotent)."""
    for statement in LEDGER_SCHEMA:
        cursor.execute(statement)


def begin_write(conn: sqlite3.Connection) -> None:
    """Take the write lock before reading ledger state that a write depends on."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def normalise_key(record_key: str) -> str:
    """Return a safe, bounded ledger key; odd or long keys are hashed."""
    text = str(record_key).strip()
    if not text:
        raise ValueError("record_key is required")
    if _SAFE_KEY_RE.fullmatch(text):
        return text
    return "h:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:40]


def _require_user(user_id: str) -> str:
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id is required for provenance records")
    return user_id.strip()


def _entry_from_row(row: Sequence[Any]) -> LedgerEntry | None:
    try:
        provenance = Provenance(
            kind=RecordKind(row[11]),
            source=row[12],
            transformation_version=row[16],
            ingested_at=row[15],
            external_id=row[13],
            source_timestamp=row[14],
            confidence=row[17],
        )
        content = json.loads(row[10]) if row[10] else None
    except (ValueError, TypeError):
        logger.warning("Skipping unreadable provenance revision %s", row[0])
        return None
    return LedgerEntry(
        id=int(row[0]),
        record_type=row[1],
        record_key=row[2],
        revision=int(row[3]),
        applied=bool(row[4]),
        supersedes_id=row[5],
        supersedes_revision=row[6],
        held_by_id=row[7],
        note=row[8],
        content_hash=row[9],
        content=content,
        provenance=provenance,
    )


def _current(
    conn: sqlite3.Connection, user_id: str, record_type: str, record_key: str
) -> LedgerEntry | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} {_FROM} "
        "WHERE p.user_id = ? AND p.record_type = ? AND p.record_key = ? AND p.applied = 1 "
        "ORDER BY p.revision DESC LIMIT 1",
        (user_id, record_type, record_key),
    ).fetchone()
    return _entry_from_row(row) if row else None


def record_revision(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    record_type: RecordType | str,
    record_key: str,
    provenance: Provenance,
    content: Any,
    retain_content: bool = False,
    note: str | None = None,
) -> RevisionOutcome:
    """Append a revision of a record, preserving the revision it supersedes.

    * Identical content from the same kind and source is ``unchanged`` (an
      idempotent re-import adds no history).
    * A lower-authority value (an automated import) cannot replace a current
      user-entered value; it is stored as a ``held`` revision for audit and the
      caller must leave the record untouched.
    * Otherwise the revision is ``recorded`` and links to the previous one.
    """
    uid = _require_user(user_id)
    record_family = RecordType(record_type)
    if provenance.kind not in ALLOWED_KINDS[record_family]:
        raise ValueError(
            f"{record_family.value} records cannot have {provenance.kind.value} provenance"
        )
    rtype = record_family.value
    key = normalise_key(record_key)
    digest = content_hash(content)

    begin_write(conn)
    current = _current(conn, uid, rtype, key)
    if (
        current is not None
        and current.provenance.kind is provenance.kind
        and current.provenance.source == provenance.source
        and current.content_hash == digest
    ):
        return RevisionOutcome("unchanged", current.id, current.revision)

    held = False
    if current is not None and not provenance.may_replace(current.provenance.kind):
        held = True
        existing = conn.execute(
            "SELECT id, revision FROM record_provenance "
            "WHERE user_id = ? AND record_type = ? AND record_key = ? "
            "AND applied = 0 AND held_by_id = ? AND content_hash = ? AND source = ?",
            (uid, rtype, key, current.id, digest, provenance.source),
        ).fetchone()
        if existing:
            return RevisionOutcome("held", int(existing[0]), int(existing[1]))

    revision = int(
        conn.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM record_provenance "
            "WHERE user_id = ? AND record_type = ? AND record_key = ?",
            (uid, rtype, key),
        ).fetchone()[0]
    )
    cursor = conn.execute(
        """
        INSERT INTO record_provenance
            (user_id, record_type, record_key, revision, kind, source, external_id,
             source_timestamp, ingested_at, transformation_version, confidence,
             applied, supersedes_id, held_by_id, lineage_note, content_hash, retained_content)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uid,
            rtype,
            key,
            revision,
            provenance.kind.value,
            provenance.source,
            provenance.external_id,
            provenance.source_timestamp,
            provenance.ingested_at,
            provenance.transformation_version,
            provenance.confidence,
            0 if held else 1,
            None if held or current is None else current.id,
            current.id if held and current is not None else None,
            HELD_NOTE if held else clean_note(note),
            digest,
            json.dumps(content, sort_keys=True, default=str) if retain_content else None,
        ),
    )
    return RevisionOutcome(
        "held" if held else "recorded", int(cursor.lastrowid or 0), revision
    )


def ensure_baseline(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    record_type: RecordType | str,
    record_key: str,
    content: Any,
    retain_content: bool = False,
    kind: RecordKind = RecordKind.IMPORTED,
) -> bool:
    """Capture a pre-ledger value as revision 1 before it is replaced.

    Without this, the first correction of a record stored before provenance
    tracking would erase the only copy of its original value. The baseline is
    labelled as an unattributed legacy revision rather than guessing a source.
    """
    uid = _require_user(user_id)
    rtype = RecordType(record_type).value
    key = normalise_key(record_key)
    begin_write(conn)
    if conn.execute(
        "SELECT 1 FROM record_provenance "
        "WHERE user_id = ? AND record_type = ? AND record_key = ? LIMIT 1",
        (uid, rtype, key),
    ).fetchone():
        return False
    record_revision(
        conn,
        user_id=uid,
        record_type=rtype,
        record_key=key,
        provenance=unattributed(kind, transformation_version=LEGACY_TRANSFORMATION_VERSION),
        content=content,
        retain_content=retain_content,
        note=BASELINE_NOTE,
    )
    return True


def current_entries(
    conn: sqlite3.Connection,
    user_id: str,
    record_type: RecordType | str,
    record_keys: Iterable[str],
) -> dict[str, LedgerEntry]:
    """Return the current (latest applied) revision, keyed by the caller's keys."""
    uid = _require_user(user_id)
    rtype = RecordType(record_type).value
    originals = {normalise_key(key): key for key in record_keys}
    keys = list(originals)
    entries: dict[str, LedgerEntry] = {}
    for start in range(0, len(keys), _IN_CHUNK):
        chunk = keys[start : start + _IN_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT {_COLUMNS} {_FROM} "
            "WHERE p.user_id = ? AND p.record_type = ? AND p.applied = 1 "
            f"AND p.record_key IN ({placeholders}) "
            "AND NOT EXISTS (SELECT 1 FROM record_provenance n "
            "WHERE n.user_id = p.user_id AND n.record_type = p.record_type "
            "AND n.record_key = p.record_key AND n.applied = 1 AND n.revision > p.revision)",
            (uid, rtype, *chunk),
        ).fetchall()
        for row in rows:
            entry = _entry_from_row(row)
            if entry is not None:
                entries[originals[entry.record_key]] = entry
    return entries


def current_entry(
    conn: sqlite3.Connection,
    user_id: str,
    record_type: RecordType | str,
    record_key: str,
) -> LedgerEntry | None:
    return current_entries(conn, user_id, record_type, [record_key]).get(record_key)


def lineage(
    conn: sqlite3.Connection,
    user_id: str,
    record_type: RecordType | str,
    record_key: str,
) -> list[LedgerEntry]:
    """Return every revision of one record, newest first, including held imports."""
    uid = _require_user(user_id)
    rtype = RecordType(record_type).value
    key = normalise_key(record_key)
    rows = conn.execute(
        f"SELECT {_COLUMNS} {_FROM} "
        "WHERE p.user_id = ? AND p.record_type = ? AND p.record_key = ? "
        "ORDER BY p.revision DESC",
        (uid, rtype, key),
    ).fetchall()
    return [entry for entry in map(_entry_from_row, rows) if entry is not None]


def summarise(
    conn: sqlite3.Connection, user_id: str, record_type: RecordType | str
) -> dict[str, Any]:
    """Aggregate the current revisions of one record type by kind and source."""
    uid = _require_user(user_id)
    rtype = RecordType(record_type).value
    rows = conn.execute(
        """
        SELECT p.kind, p.source, COUNT(*), MAX(p.ingested_at)
        FROM record_provenance p
        WHERE p.user_id = ? AND p.record_type = ? AND p.applied = 1
          AND NOT EXISTS (
              SELECT 1 FROM record_provenance n
              WHERE n.user_id = p.user_id AND n.record_type = p.record_type
                AND n.record_key = p.record_key AND n.applied = 1
                AND n.revision > p.revision)
        GROUP BY p.kind, p.source
        ORDER BY p.kind, p.source
        """,
        (uid, rtype),
    ).fetchall()
    sources = [
        {
            "kind": kind,
            "source": source,
            "source_label": source_label(source),
            "records": int(count),
            "latest_ingested_at": latest,
        }
        for kind, source, count, latest in rows
    ]
    by_kind: dict[str, int] = {}
    for item in sources:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + item["records"]
    return {
        "record_type": rtype,
        "total": sum(by_kind.values()),
        "by_kind": by_kind,
        "latest_ingested_at": max((i["latest_ingested_at"] for i in sources), default=None),
        "sources": sources,
    }
