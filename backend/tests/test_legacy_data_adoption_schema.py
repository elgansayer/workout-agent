"""``tools/legacy_data_migration.py`` must accept the schema production really has.

Production does not only run ``database.init_db()``: ``CSRFMiddleware`` creates
``csrf_tokens`` (which has a ``user_id`` column) in the same database on first
use. A schema check proven only against ``init_db()`` would refuse the real
database, so these tests build the production shape with the production code.
Fixtures are synthetic.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import database
from cryptography.fernet import Fernet
from webapp.csrf_security import CSRFTokenStore

from tools.backup_restore import Keyring
from tools.legacy_data_migration import EPHEMERAL_TABLES, USER_OWNED_TABLES, migrate

BACKEND_DIR = Path(__file__).resolve().parents[1]
# Every non-test module that issues ``CREATE TABLE``. ``health_schema.py`` is not
# applied to the production database yet.
REVIEWED_DDL_SOURCES = {
    "account_recovery.py",
    "database.py",
    "health_schema.py",
    "provenance_ledger.py",
    "webapp/csrf_security.py",
    # Consumed OAuth state hashes: tenant-bound, no user_id column and short
    # lived, so they are neither adopted nor treated as single-user data.
    "webapp/oauth_state.py",
}
_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?\w+\s*\(", re.IGNORECASE
)


def _production_database(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """``init_db()`` plus the table ``CSRFMiddleware`` adds, with tokens issued."""
    db_file = tmp_path / "workout_agent.db"
    database.init_db(str(db_file))
    ids = {"legacy": database.get_legacy_user_id(str(db_file))}
    for name in ("target", "other"):
        ids[name] = database.get_or_create_user(
            f"{name}@example.invalid", name.title(), db_path=str(db_file)
        )["id"]
    store = CSRFTokenStore(str(db_file), ttl_seconds=600)
    store.issue(ids["target"], "synthetic-session-cookie-a")
    store.issue(ids["other"], "synthetic-session-cookie-b")
    return db_file, ids


def _tenant_tables(db_file: Path) -> set[str]:
    with sqlite3.connect(db_file) as conn:
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name != 'users'"
            )
        ]
        return {
            name
            for name in names
            if any(
                column[1] == "user_id"
                for column in conn.execute(f'PRAGMA table_info("{name}")')
            )
        }


def _csrf_rows(db_file: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(db_file) as conn:
        return conn.execute("SELECT * FROM csrf_tokens ORDER BY token_hash").fetchall()


def test_every_production_tenant_table_is_classified_by_the_tool(
    tmp_path: Path,
) -> None:
    db_file, _ids = _production_database(tmp_path)

    assert set(USER_OWNED_TABLES).isdisjoint(EPHEMERAL_TABLES)
    assert _tenant_tables(db_file) == set(USER_OWNED_TABLES) | set(EPHEMERAL_TABLES)


def test_adoption_runs_on_the_production_schema_and_leaves_csrf_tokens_alone(
    tmp_path: Path,
) -> None:
    db_file, ids = _production_database(tmp_path)
    database.save_notification(
        ids["legacy"], title="legacy", message="legacy", db_path=str(db_file)
    )
    tokens = _csrf_rows(db_file)
    assert len(tokens) == 2
    keyring = Keyring(keys={"test": Fernet.generate_key()}, active_key_id="test")

    evidence = migrate(
        db_file,
        target_user_id=ids["target"],
        backup_output=tmp_path / "pre-migration.wab",
        keyring=keyring,
    )

    assert evidence["status"] == "migrated"
    assert evidence["rows_reassigned"] >= 1
    assert "csrf_tokens" not in evidence["before"]  # session state is not user data
    assert _csrf_rows(db_file) == tokens  # tokens keep their session-bound owners
    assert database.get_notifications(ids["target"], db_path=str(db_file))
    assert not database.get_notifications(ids["other"], db_path=str(db_file))
    again = migrate(
        db_file,
        target_user_id=ids["target"],
        backup_output=tmp_path / "again.wab",
        keyring=keyring,
    )
    assert again["status"] == "noop"
    assert _csrf_rows(db_file) == tokens


def test_no_unreviewed_module_creates_tables_in_the_production_database() -> None:
    sources = {
        relative.as_posix()
        for path in BACKEND_DIR.rglob("*.py")
        if (relative := path.relative_to(BACKEND_DIR)).parts[0] != "tests"
        and _CREATE_TABLE.search(path.read_text(encoding="utf-8"))
    }

    assert sources == REVIEWED_DDL_SOURCES, (
        "A module that creates tables changed. Decide whether its user_id tables "
        "need adopting by tools/legacy_data_migration.py (USER_OWNED_TABLES) or "
        "are transient session state (EPHEMERAL_TABLES), then update this list."
    )
