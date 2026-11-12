"""Persisted connector operation state is tenant scoped, bounded and truthful."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import database
import pytest
from connector_operations import (
    OPERATION_LEASE,
    ConnectorStatus,
    OperationBusyError,
    OperationPhase,
    SyncOutcome,
    begin_operation,
    clear_sync_outcome,
    finish_operation,
    read_connector_status,
    record_sync_outcome,
    tracked_operation,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "connector.db")
    database.init_db(path)
    return path


@pytest.fixture()
def alice(db: str) -> str:
    return database.get_or_create_user("alice@example.test", "Alice", db)["id"]


@pytest.fixture()
def bob(db: str) -> str:
    return database.get_or_create_user("bob@example.test", "Bob", db)["id"]


def _raw_meta(db: str, user_id: str) -> dict[str, str]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT key, value FROM hevy_meta WHERE user_id = ?", (user_id,)
        ).fetchall()
    return dict(rows)


def test_a_tenant_with_no_records_reads_as_idle(db: str, alice: str) -> None:
    status = read_connector_status(alice, db_path=db, now=NOW)

    assert status == ConnectorStatus(provider="hevy")
    assert status.as_dict()["operation"] is None
    assert status.as_dict()["last_sync"] is None


def test_a_started_operation_is_live_until_it_finishes(db: str, alice: str) -> None:
    token = begin_operation(alice, OperationPhase.SYNCING, db_path=db, now=NOW)

    live = read_connector_status(alice, db_path=db, now=NOW + timedelta(minutes=2))
    assert live.operation is OperationPhase.SYNCING
    assert live.operation_started_at == NOW
    assert live.operation_stalled is False

    finish_operation(alice, token, db_path=db)
    assert read_connector_status(alice, db_path=db, now=NOW).operation is None


def test_a_second_operation_cannot_start_while_one_is_live(db: str, alice: str) -> None:
    begin_operation(alice, OperationPhase.SYNCING, db_path=db, now=NOW)

    with pytest.raises(OperationBusyError) as excinfo:
        begin_operation(
            alice,
            OperationPhase.CONNECTING,
            db_path=db,
            now=NOW + timedelta(minutes=1),
        )

    assert excinfo.value.phase is OperationPhase.SYNCING


def test_an_expired_lease_is_reported_stalled_and_can_be_taken_over(
    db: str, alice: str
) -> None:
    begin_operation(alice, OperationPhase.SYNCING, db_path=db, now=NOW)
    later = NOW + OPERATION_LEASE + timedelta(seconds=1)

    stalled = read_connector_status(alice, db_path=db, now=later)
    assert stalled.operation is None
    assert stalled.operation_stalled is True
    assert stalled.operation_started_at == NOW

    token = begin_operation(alice, OperationPhase.SYNCING, db_path=db, now=later)
    assert token == later.isoformat()
    renewed = read_connector_status(alice, db_path=db, now=later)
    assert renewed.operation is OperationPhase.SYNCING
    assert renewed.operation_stalled is False


def test_a_slow_worker_cannot_clear_a_newer_operations_lease(
    db: str, alice: str
) -> None:
    old_token = begin_operation(alice, OperationPhase.SYNCING, db_path=db, now=NOW)
    later = NOW + OPERATION_LEASE + timedelta(minutes=1)
    begin_operation(alice, OperationPhase.SYNCING, db_path=db, now=later)

    finish_operation(alice, old_token, db_path=db)

    assert (
        read_connector_status(alice, db_path=db, now=later).operation
        is OperationPhase.SYNCING
    )


def test_operations_are_tenant_scoped(db: str, alice: str, bob: str) -> None:
    begin_operation(alice, OperationPhase.SYNCING, db_path=db, now=NOW)
    record_sync_outcome(alice, SyncOutcome.FAILED, code="hevy_unavailable", db_path=db)

    assert read_connector_status(bob, db_path=db, now=NOW) == ConnectorStatus()
    # Alice's lease never blocks Bob.
    begin_operation(bob, OperationPhase.SYNCING, db_path=db, now=NOW)
    finish_operation(bob, NOW.isoformat(), db_path=db)
    assert (
        read_connector_status(alice, db_path=db, now=NOW).operation
        is OperationPhase.SYNCING
    )
    assert _raw_meta(db, bob) == {}
    clear_sync_outcome(bob, db_path=db)
    assert read_connector_status(alice, db_path=db, now=NOW).last_sync_failed


def test_a_sync_outcome_keeps_a_fixed_code_never_provider_text(
    db: str, alice: str
) -> None:
    record_sync_outcome(
        alice,
        SyncOutcome.FAILED,
        code="Hevy said: key sk-secret-123 is invalid",
        db_path=db,
        now=NOW,
    )

    stored = _raw_meta(db, alice)["connector:hevy:last_sync"]
    assert "secret" not in stored
    status = read_connector_status(alice, db_path=db, now=NOW)
    assert status.last_sync_failed
    assert status.last_sync_code == "sync_failed"
    assert status.last_sync_at == NOW


def test_a_successful_sync_records_the_count_and_clears_the_failure_code(
    db: str, alice: str
) -> None:
    record_sync_outcome(alice, SyncOutcome.FAILED, code="hevy_unavailable", db_path=db)
    record_sync_outcome(alice, SyncOutcome.SUCCEEDED, records=7, db_path=db, now=NOW)

    status = read_connector_status(alice, db_path=db, now=NOW)

    assert status.last_sync_succeeded
    assert status.last_sync_records == 7
    assert status.last_sync_code is None
    assert status.as_dict()["last_sync"] == {
        "outcome": "succeeded",
        "at": NOW.isoformat(),
        "code": None,
        "records": 7,
    }


def test_clearing_the_outcome_forgets_only_the_last_sync(db: str, alice: str) -> None:
    begin_operation(alice, OperationPhase.SYNCING, db_path=db, now=NOW)
    record_sync_outcome(alice, SyncOutcome.FAILED, code="sync_failed", db_path=db)

    clear_sync_outcome(alice, db_path=db)

    status = read_connector_status(alice, db_path=db, now=NOW)
    assert status.last_sync_outcome is None
    assert status.operation is OperationPhase.SYNCING


@pytest.mark.parametrize(
    "raw",
    ["not json", "[]", '"text"', json.dumps({"phase": "exploding", "started_at": "x"}),
     json.dumps({"phase": "syncing"}), json.dumps({"phase": "syncing", "started_at": "garbage"})],
)
def test_malformed_operation_records_read_as_absent(
    db: str, alice: str, raw: str
) -> None:
    database.set_meta("connector:hevy:operation", raw, db, user_id=alice)

    status = read_connector_status(alice, db_path=db, now=NOW)

    assert status.operation is None
    assert status.operation_stalled is False


@pytest.mark.parametrize(
    "raw",
    ["not json", json.dumps({"outcome": "maybe"}), json.dumps({"at": "2026-09-01"})],
)
def test_malformed_sync_records_read_as_absent(db: str, alice: str, raw: str) -> None:
    database.set_meta("connector:hevy:last_sync", raw, db, user_id=alice)

    assert read_connector_status(alice, db_path=db, now=NOW).last_sync_outcome is None


def test_an_unknown_provider_name_is_rejected(db: str, alice: str) -> None:
    with pytest.raises(ValueError):
        read_connector_status(alice, provider="hevy:other", db_path=db)


def test_a_missing_tenant_is_refused_rather_than_widened(
    db: str, monkeypatch: Any
) -> None:
    from tenancy import TenantRequiredError

    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(TenantRequiredError):
        begin_operation(None, OperationPhase.SYNCING, db_path=db)  # type: ignore[arg-type]


def test_a_tracked_sync_records_its_outcome_and_releases_the_lease(
    db: str, alice: str
) -> None:
    with tracked_operation(alice, OperationPhase.SYNCING, db_path=db, now=NOW) as op:
        during = read_connector_status(alice, db_path=db, now=NOW)
        op.succeeded(records=12)

    after = read_connector_status(alice, db_path=db)
    assert during.operation is OperationPhase.SYNCING
    assert after.operation is None
    assert after.last_sync_succeeded
    assert after.last_sync_records == 12


def test_a_tracked_sync_that_raises_is_recorded_as_failed_and_re_raised(
    db: str, alice: str
) -> None:
    with (
        pytest.raises(RuntimeError),
        tracked_operation(alice, OperationPhase.SYNCING, db_path=db, now=NOW),
    ):
        raise RuntimeError("provider exploded with secret-token")

    status = read_connector_status(alice, db_path=db)
    assert status.operation is None
    assert status.last_sync_failed
    assert status.last_sync_code == "unexpected_error"
    assert "secret" not in "".join(_raw_meta(db, alice).values())


def test_a_tracked_sync_keeps_the_failure_code_the_caller_chose(
    db: str, alice: str
) -> None:
    with tracked_operation(alice, OperationPhase.SYNCING, db_path=db) as op:
        op.failed("hevy_unavailable")

    assert (
        read_connector_status(alice, db_path=db).last_sync_code == "hevy_unavailable"
    )


def test_a_tracked_connection_check_leaves_no_sync_record(db: str, alice: str) -> None:
    record_sync_outcome(alice, SyncOutcome.SUCCEEDED, records=3, db_path=db, now=NOW)

    with tracked_operation(alice, OperationPhase.CONNECTING, db_path=db):
        assert (
            read_connector_status(alice, db_path=db).operation
            is OperationPhase.CONNECTING
        )

    status = read_connector_status(alice, db_path=db)
    assert status.operation is None
    assert status.last_sync_records == 3


def test_a_busy_tenant_cannot_start_a_tracked_sync(db: str, alice: str) -> None:
    with tracked_operation(alice, OperationPhase.SYNCING, db_path=db):
        with (
            pytest.raises(OperationBusyError),
            tracked_operation(alice, OperationPhase.SYNCING, db_path=db),
        ):
            raise AssertionError("must not run")  # pragma: no cover
        # The refused attempt must not release the running sync's lease.
        assert (
            read_connector_status(alice, db_path=db).operation
            is OperationPhase.SYNCING
        )


def test_a_bookkeeping_failure_never_masks_the_syncs_own_result(
    db: str, alice: str, monkeypatch: Any
) -> None:
    import connector_operations

    def _broken(*_a: Any, **_k: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    with tracked_operation(alice, OperationPhase.SYNCING, db_path=db) as op:
        op.succeeded(records=1)
        monkeypatch.setattr(connector_operations, "finish_operation", _broken)
    # Leaving the block did not raise; the lease simply expires on its own.
    monkeypatch.undo()
    stalled = read_connector_status(
        alice, db_path=db, now=datetime.now(tz=timezone.utc) + OPERATION_LEASE * 2
    )
    assert stalled.operation_stalled is True
