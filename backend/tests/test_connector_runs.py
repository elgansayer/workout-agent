"""Tenant-scoped connector run ledger persistence."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import database
import pytest

NOW = datetime(2035, 1, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def ledger(tmp_path: Path) -> tuple[str, str, str]:
    db = str(tmp_path / "runs.db")
    database.init_db(db)
    first = database.get_or_create_user("a@example.test", "A", db_path=db)["id"]
    second = database.get_or_create_user("b@example.test", "B", db_path=db)["id"]
    return db, first, second


def test_schema_is_tenant_scoped_indexed_and_idempotent(
    ledger: tuple[str, str, str],
) -> None:
    db, _, _ = ledger
    database.init_db(db)
    database.init_db(db)
    with sqlite3.connect(db) as conn:
        foreign_keys = conn.execute(
            'SELECT "table", "from" FROM pragma_foreign_key_list(?)', ("connector_runs",)
        ).fetchall()
        assert ("users", "user_id") in foreign_keys
        indexes = [
            [
                column[0]
                for column in conn.execute(
                    "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (name,)
                )
            ]
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'connector_runs'"
            )
        ]
        assert ["user_id", "connector", "id"] in indexes


def test_user_id_is_immutable(ledger: tuple[str, str, str]) -> None:
    db, alice, bob = ledger
    run_id = database.record_connector_run(alice, "hevy", "sync", "succeeded", now=NOW, db_path=db)
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.DatabaseError, match="immutable"):
        conn.execute("UPDATE connector_runs SET user_id = ? WHERE id = ?", (bob, run_id))


def test_exclusive_start_refuses_a_second_run_of_the_same_kind(
    ledger: tuple[str, str, str],
) -> None:
    db, alice, bob = ledger
    first = database.start_connector_run(alice, "hevy", "sync", now=NOW, db_path=db)
    assert first is not None
    later = NOW + timedelta(minutes=1)
    assert database.start_connector_run(alice, "hevy", "sync", now=later, db_path=db) is None
    # A different kind, connector or tenant is independent.
    assert database.start_connector_run(alice, "hevy", "test", now=later, db_path=db) is not None
    assert database.start_connector_run(alice, "google_health", "sync", now=later, db_path=db) is not None
    assert database.start_connector_run(bob, "hevy", "sync", now=later, db_path=db) is not None


def test_non_exclusive_start_supersedes_the_previous_attempt(
    ledger: tuple[str, str, str],
) -> None:
    db, alice, _ = ledger
    first = database.start_connector_run(alice, "google_health", "authorize", exclusive=False, now=NOW, db_path=db)
    second = database.start_connector_run(
        alice, "google_health", "authorize", exclusive=False,
        now=NOW + timedelta(minutes=1), db_path=db,
    )
    assert first is not None and second is not None and first != second
    runs = database.get_connector_runs(alice, "google_health", db)
    assert [run["id"] for run in runs] == [second]


def test_expired_lease_is_closed_as_interrupted_and_unblocks_new_work(
    ledger: tuple[str, str, str],
) -> None:
    db, alice, _ = ledger
    stuck = database.start_connector_run(alice, "hevy", "sync", now=NOW, db_path=db)
    assert stuck is not None
    assert database.start_connector_run(alice, "hevy", "sync", lease_seconds=900,
                                        now=NOW + timedelta(minutes=14), db_path=db) is None
    fresh = database.start_connector_run(alice, "hevy", "sync", lease_seconds=900,
                                         now=NOW + timedelta(minutes=16), db_path=db)
    assert fresh is not None
    first, second = database.get_connector_runs(alice, "hevy", db)
    assert (first["status"], first["error_code"], first["retryable"]) == ("failed", "interrupted", True)
    assert second["status"] == "running"
    # The vanished worker finishing late cannot overwrite what was recorded.
    assert not database.finish_connector_run(alice, stuck, "succeeded", db_path=db)


def test_finish_is_first_writer_wins_and_tenant_checked(
    ledger: tuple[str, str, str],
) -> None:
    db, alice, bob = ledger
    run_id = database.start_connector_run(alice, "hevy", "sync", now=NOW, db_path=db)
    assert run_id is not None
    assert not database.finish_connector_run(bob, run_id, "failed", error_code="x", db_path=db)
    assert database.finish_connector_run(
        alice, run_id, "partial", error_code="partial_import", retryable=True,
        fetched=10, written=6, skipped=1, now=NOW + timedelta(minutes=1), db_path=db,
    )
    assert not database.finish_connector_run(alice, run_id, "succeeded", fetched=10, db_path=db)
    (run,) = database.get_connector_runs(alice, "hevy", db)
    assert (run["status"], run["error_code"], run["fetched"], run["written"], run["skipped"]) == (
        "partial", "partial_import", 10, 6, 1,
    )
    assert run["finished_at"] == (NOW + timedelta(minutes=1)).isoformat()
    assert database.get_connector_runs(bob, "hevy", db) == []


def test_invalid_kinds_and_statuses_are_rejected(ledger: tuple[str, str, str]) -> None:
    db, alice, _ = ledger
    with pytest.raises(ValueError):
        database.start_connector_run(alice, "hevy", "bogus", db_path=db)
    with pytest.raises(ValueError):
        database.record_connector_run(alice, "hevy", "sync", "running", db_path=db)
    with pytest.raises(ValueError):
        database.finish_connector_run(alice, 1, "running", db_path=db)


def test_timestamps_are_stored_as_utc_whatever_the_input_zone(
    ledger: tuple[str, str, str],
) -> None:
    db, alice, _ = ledger
    plus_two = timezone(timedelta(hours=2))
    database.record_connector_run(alice, "hevy", "test", "succeeded", now=NOW.astimezone(plus_two), db_path=db)
    database.record_connector_run(alice, "hevy", "test", "succeeded", now=NOW.replace(tzinfo=None), db_path=db)
    stored = [run["started_at"] for run in database.get_connector_runs(alice, "hevy", db)]
    assert stored == [NOW.isoformat(), NOW.isoformat()]


def test_retention_is_bounded_but_keeps_the_latest_successes(
    ledger: tuple[str, str, str],
) -> None:
    db, alice, bob = ledger
    database.record_connector_run(bob, "hevy", "sync", "succeeded", now=NOW, db_path=db)
    database.record_connector_run(alice, "hevy", "sync", "succeeded", now=NOW, db_path=db)
    for index in range(120):
        database.record_connector_run(
            alice, "hevy", "test", "failed", error_code="provider_unavailable",
            retryable=True, now=NOW + timedelta(minutes=index + 1), db_path=db,
        )
    runs = database.get_connector_runs(alice, "hevy", db)
    assert len(runs) == database.CONNECTOR_RUN_KEEP_RECENT + 1
    assert runs[0]["status"] == "succeeded"  # an old success survives a long failure streak
    assert len(database.get_connector_runs(bob, "hevy", db)) == 1  # other tenants untouched


def test_running_rows_are_never_pruned(ledger: tuple[str, str, str]) -> None:
    db, alice, _ = ledger
    running = database.start_connector_run(alice, "hevy", "sync", now=NOW, db_path=db)
    for index in range(80):
        database.record_connector_run(
            alice, "hevy", "test", "failed", now=NOW + timedelta(seconds=index), db_path=db
        )
    assert running in [run["id"] for run in database.get_connector_runs(alice, "hevy", db)]


def test_delete_is_scoped_to_tenant_and_connector(ledger: tuple[str, str, str]) -> None:
    db, alice, bob = ledger
    for user in (alice, bob):
        database.record_connector_run(user, "hevy", "sync", "succeeded", now=NOW, db_path=db)
    database.record_connector_run(alice, "google_health", "sync", "succeeded", now=NOW, db_path=db)
    assert database.delete_connector_runs(alice, "hevy", db) == 1
    assert database.get_connector_runs(alice, "hevy", db) == []
    assert len(database.get_connector_runs(alice, "google_health", db)) == 1
    assert len(database.get_connector_runs(bob, "hevy", db)) == 1


def test_credential_presence_never_decrypts_and_is_tenant_scoped(
    ledger: tuple[str, str, str],
) -> None:
    db, alice, bob = ledger
    database.save_user_api_key(alice, "hevy", "synthetic-key", db_path=db)
    database.save_user_api_key(alice, "claude", "", db_path=db)  # blank key is not "present"
    presence = database.get_credential_presence(alice, db)
    assert set(presence) == {"hevy"}
    assert "synthetic-key" not in "".join(presence.values())
    assert database.get_credential_presence(bob, db) == {}
