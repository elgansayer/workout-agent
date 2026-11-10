"""Persistence and lifecycle tests for account export jobs and audit events."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import account_export_store as store
import pytest
from database import get_or_create_user, init_db

NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
WINDOW = timedelta(hours=24)


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "store.db")
    init_db(path)
    return path


@pytest.fixture()
def users(db_path: str) -> tuple[str, str]:
    alpha = get_or_create_user("alpha@example.com", "Alpha", db_path=db_path)["id"]
    bravo = get_or_create_user("bravo@example.com", "Bravo", db_path=db_path)["id"]
    return alpha, bravo


def _new_id() -> str:
    return uuid.uuid4().hex


def _create(
    db_path: str,
    user_id: str,
    *,
    now: datetime = NOW,
    max_requests: int = 5,
) -> tuple[store.ExportJob, bool]:
    return store.create_job(
        user_id,
        _new_id(),
        schema_version=1,
        now=now,
        max_requests=max_requests,
        window=WINDOW,
        db_path=db_path,
    )


def _ready(db_path: str, user_id: str, *, now: datetime = NOW, ttl_hours: int = 24) -> str:
    job, _ = _create(db_path, user_id, now=now)
    assert store.claim_job(job.id, now=now, db_path=db_path) is not None
    assert store.complete_job(
        job.id,
        now=now,
        expires_at=now + timedelta(hours=ttl_hours),
        size_bytes=10,
        checksum_sha256="0" * 64,
        record_counts={"profile.account": 1},
        db_path=db_path,
    )
    return job.id


def _events(db_path: str, user_id: str) -> list[tuple[str, str, str]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT event_type, outcome, resource_id FROM account_audit_events "
            "WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _user_id_is_indexed_first(conn: sqlite3.Connection, table: str) -> bool:
    for (index_name,) in conn.execute("SELECT name FROM pragma_index_list(?)", (table,)):
        columns = conn.execute(
            "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index_name,)
        ).fetchall()
        if columns and columns[0][0] == "user_id":
            return True
    return False


def test_new_tables_are_tenant_scoped_indexed_and_idempotent(db_path: str) -> None:
    init_db(db_path)
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        for table in ("account_exports", "account_audit_events"):
            assert _user_id_is_indexed_first(conn, table), table
            references = {
                row[3]: row[2]
                for row in conn.execute("SELECT * FROM pragma_foreign_key_list(?)", (table,))
            }
            assert references == {"user_id": "users"}
            columns = {row[1]: row[3] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert columns["user_id"] == 1  # NOT NULL


def test_user_id_of_an_export_job_is_immutable(db_path: str, users: tuple[str, str]) -> None:
    job, _ = _create(db_path, users[0])
    with sqlite3.connect(db_path) as conn, pytest.raises(sqlite3.DatabaseError, match="immutable"):
        conn.execute("UPDATE account_exports SET user_id = ? WHERE id = ?", (users[1], job.id))


def test_audit_events_are_append_only_but_can_be_deleted(
    db_path: str, users: tuple[str, str]
) -> None:
    _create(db_path, users[0])
    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute("UPDATE account_audit_events SET outcome = 'tampered'")
        # Deletion stays possible so retention and account-deletion flows work.
        conn.execute("DELETE FROM account_audit_events WHERE user_id = ?", (users[0],))
        assert conn.execute("SELECT COUNT(*) FROM account_audit_events").fetchone() == (0,)


def test_database_allows_only_one_in_flight_job_per_account(
    db_path: str, users: tuple[str, str]
) -> None:
    _create(db_path, users[0])
    with sqlite3.connect(db_path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO account_exports "
            "(id, user_id, status, schema_version, requested_at, heartbeat_at) "
            "VALUES (?, ?, 'queued', 1, 'x', 'x')",
            (_new_id(), users[0]),
        )
    # A different account is unaffected.
    other, created = _create(db_path, users[1])
    assert created and other.user_id == users[1]


# ---------------------------------------------------------------------------
# Creating and finding jobs
# ---------------------------------------------------------------------------


def test_create_job_queues_a_job_and_audits_the_request(
    db_path: str, users: tuple[str, str]
) -> None:
    job, created = _create(db_path, users[0])
    assert created
    assert job.status == store.QUEUED
    assert job.user_id == users[0]
    assert job.requested_at == "2026-11-10T12:00:00+00:00"
    assert job.expires_at is None
    assert _events(db_path, users[0]) == [
        (store.EVENT_REQUESTED, store.OUTCOME_SUCCESS, job.id)
    ]
    assert _events(db_path, users[1]) == []


def test_second_request_returns_the_job_already_in_flight(
    db_path: str, users: tuple[str, str]
) -> None:
    first, created_first = _create(db_path, users[0])
    again, created_again = _create(db_path, users[0], now=NOW + timedelta(seconds=5))
    assert created_first and not created_again
    assert again.id == first.id
    assert len(_events(db_path, users[0])) == 1
    store.claim_job(first.id, now=NOW, db_path=db_path)
    running, created_running = _create(db_path, users[0])
    assert running.id == first.id and not created_running


def test_a_racing_insert_returns_the_job_that_won(
    db_path: str, users: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two replicas can both pass the in-flight check; the index decides."""

    winner, _ = _create(db_path, users[0])
    real_lookup = store._fetch_in_flight
    calls = {"count": 0}

    def blind_first_lookup(conn: sqlite3.Connection, user_id: str) -> store.ExportJob | None:
        calls["count"] += 1
        return None if calls["count"] == 1 else real_lookup(conn, user_id)

    monkeypatch.setattr(store, "_fetch_in_flight", blind_first_lookup)
    job, created = _create(db_path, users[0])
    assert not created and job.id == winner.id
    assert calls["count"] == 2
    assert len(_events(db_path, users[0])) == 1  # the loser audited nothing


def test_a_new_request_is_allowed_once_the_previous_job_finished(
    db_path: str, users: tuple[str, str]
) -> None:
    first_id = _ready(db_path, users[0])
    second, created = _create(db_path, users[0], now=NOW + timedelta(minutes=1))
    assert created and second.id != first_id


def test_request_limit_applies_per_account_within_the_window(
    db_path: str, users: tuple[str, str]
) -> None:
    for index in range(3):
        _ready(db_path, users[0], now=NOW + timedelta(minutes=index))
    with pytest.raises(store.ExportRateLimited) as excinfo:
        _create(db_path, users[0], now=NOW + timedelta(minutes=10), max_requests=3)
    expected = int((WINDOW - timedelta(minutes=10)).total_seconds())
    assert excinfo.value.retry_after_seconds == expected
    # The other account has its own allowance, and the window slides.
    _create(db_path, users[1], max_requests=3)
    later, created = _create(
        db_path, users[0], now=NOW + WINDOW + timedelta(minutes=1), max_requests=3
    )
    assert created and later.status == store.QUEUED


@pytest.mark.parametrize("bad_id", ["", "short", "G" * 32, "A" * 32, "../" + "a" * 29])
def test_create_job_rejects_malformed_ids(
    db_path: str, users: tuple[str, str], bad_id: str
) -> None:
    with pytest.raises(ValueError, match="hexadecimal"):
        store.create_job(
            users[0],
            bad_id,
            schema_version=1,
            now=NOW,
            max_requests=5,
            window=WINDOW,
            db_path=db_path,
        )


def test_create_job_requires_an_owner(db_path: str) -> None:
    with pytest.raises(ValueError, match="owner"):
        _create(db_path, "")


def test_jobs_are_invisible_to_other_accounts(
    db_path: str, users: tuple[str, str]
) -> None:
    job, _ = _create(db_path, users[0])
    assert store.get_job(users[0], job.id, db_path=db_path) is not None
    assert store.get_job(users[1], job.id, db_path=db_path) is None
    assert store.list_jobs(users[1], db_path=db_path) == []
    assert not store.delete_job(users[1], job.id, now=NOW, db_path=db_path)
    assert store.get_job(users[0], job.id, db_path=db_path).status == store.QUEUED
    assert store.get_job("", job.id, db_path=db_path) is None
    assert store.get_job(users[0], "../etc/passwd", db_path=db_path) is None


def test_list_jobs_is_newest_first_and_hides_deleted(
    db_path: str, users: tuple[str, str]
) -> None:
    first = _ready(db_path, users[0], now=NOW)
    second = _ready(db_path, users[0], now=NOW + timedelta(hours=1))
    third = _ready(db_path, users[0], now=NOW + timedelta(hours=2))
    assert store.delete_job(users[0], second, now=NOW, db_path=db_path)
    listed = [job.id for job in store.list_jobs(users[0], db_path=db_path)]
    assert listed == [third, first]
    assert [job.id for job in store.list_jobs(users[0], limit=1, db_path=db_path)] == [third]


# ---------------------------------------------------------------------------
# Lifecycle transitions
# ---------------------------------------------------------------------------


def test_claim_job_only_moves_queued_jobs_and_reads_the_owner_from_the_row(
    db_path: str, users: tuple[str, str]
) -> None:
    job, _ = _create(db_path, users[0])
    claimed = store.claim_job(job.id, now=NOW + timedelta(seconds=1), db_path=db_path)
    assert claimed is not None
    assert claimed.status == store.RUNNING
    assert claimed.user_id == users[0]
    assert claimed.started_at == "2026-11-10T12:00:01+00:00"
    assert store.claim_job(job.id, now=NOW, db_path=db_path) is None
    assert store.claim_job("not-an-id", now=NOW, db_path=db_path) is None


def test_complete_job_publishes_metadata_and_audits(
    db_path: str, users: tuple[str, str]
) -> None:
    job_id = _ready(db_path, users[0])
    job = store.get_job(users[0], job_id, db_path=db_path)
    assert job is not None
    assert job.status == store.READY
    assert job.expires_at == "2026-11-11T12:00:00+00:00"
    assert (job.size_bytes, job.checksum_sha256) == (10, "0" * 64)
    assert job.record_counts == {"profile.account": 1}
    assert [event[0] for event in _events(db_path, users[0])] == [
        store.EVENT_REQUESTED,
        store.EVENT_COMPLETED,
    ]
    assert not store.complete_job(
        job_id,
        now=NOW,
        expires_at=NOW,
        size_bytes=1,
        checksum_sha256="1" * 64,
        record_counts={},
        db_path=db_path,
    )


def test_touch_job_only_refreshes_running_jobs(
    db_path: str, users: tuple[str, str]
) -> None:
    job, _ = _create(db_path, users[0])
    assert not store.touch_job(job.id, now=NOW, db_path=db_path)
    store.claim_job(job.id, now=NOW, db_path=db_path)
    assert store.touch_job(job.id, now=NOW + timedelta(seconds=30), db_path=db_path)
    refreshed = store.get_job(users[0], job.id, db_path=db_path)
    assert refreshed.heartbeat_at == "2026-11-10T12:00:30+00:00"


def test_fail_job_records_a_stable_code_and_audits(
    db_path: str, users: tuple[str, str]
) -> None:
    job, _ = _create(db_path, users[0])
    store.claim_job(job.id, now=NOW, db_path=db_path)
    assert store.fail_job(job.id, now=NOW, error_code="build_failed", db_path=db_path)
    failed = store.get_job(users[0], job.id, db_path=db_path)
    assert failed.status == store.FAILED
    assert failed.error_code == "build_failed"
    assert (store.EVENT_FAILED, store.OUTCOME_FAILURE, job.id) in _events(db_path, users[0])
    assert not store.fail_job(job.id, now=NOW, error_code="again", db_path=db_path)
    replacement, created = _create(db_path, users[0], now=NOW + timedelta(minutes=1))
    assert created and replacement.id != job.id


def test_deleting_an_in_flight_job_cancels_it(
    db_path: str, users: tuple[str, str]
) -> None:
    job, _ = _create(db_path, users[0])
    store.claim_job(job.id, now=NOW, db_path=db_path)
    assert store.delete_job(users[0], job.id, now=NOW, db_path=db_path)
    assert not store.touch_job(job.id, now=NOW, db_path=db_path)  # worker sees cancel
    assert not store.complete_job(
        job.id,
        now=NOW,
        expires_at=NOW,
        size_bytes=1,
        checksum_sha256="0" * 64,
        record_counts={},
        db_path=db_path,
    )
    assert store.get_job(users[0], job.id, db_path=db_path).status == store.DELETED
    assert not store.delete_job(users[0], job.id, now=NOW, db_path=db_path)
    assert (store.EVENT_DELETED, store.OUTCOME_SUCCESS, job.id) in _events(db_path, users[0])


def test_record_download_appends_an_owner_scoped_event(
    db_path: str, users: tuple[str, str]
) -> None:
    job_id = _ready(db_path, users[0])
    store.record_download(users[0], job_id, now=NOW, db_path=db_path)
    assert _events(db_path, users[0])[-1] == (
        store.EVENT_DOWNLOADED,
        store.OUTCOME_SUCCESS,
        job_id,
    )
    assert _events(db_path, users[1]) == []


# ---------------------------------------------------------------------------
# Expiry, staleness and pruning
# ---------------------------------------------------------------------------


def test_expire_due_expires_only_past_due_exports(
    db_path: str, users: tuple[str, str]
) -> None:
    due = _ready(db_path, users[0], now=NOW, ttl_hours=1)
    later = _ready(db_path, users[1], now=NOW, ttl_hours=48)
    expired = store.expire_due(NOW + timedelta(hours=2), db_path=db_path)
    assert [job.id for job in expired] == [due]
    assert store.get_job(users[0], due, db_path=db_path).status == store.EXPIRED
    assert store.get_job(users[1], later, db_path=db_path).status == store.READY
    assert (store.EVENT_EXPIRED, store.OUTCOME_SUCCESS, due) in _events(db_path, users[0])
    assert store.expire_due(NOW + timedelta(hours=2), db_path=db_path) == []


def test_expire_due_can_be_limited_to_one_owner(
    db_path: str, users: tuple[str, str]
) -> None:
    mine = _ready(db_path, users[0], now=NOW, ttl_hours=1)
    theirs = _ready(db_path, users[1], now=NOW, ttl_hours=1)
    later = NOW + timedelta(hours=2)
    assert [j.id for j in store.expire_due(later, user_id=users[0], db_path=db_path)] == [mine]
    assert store.get_job(users[1], theirs, db_path=db_path).status == store.READY


def test_expire_oldest_ready_keeps_the_newest(
    db_path: str, users: tuple[str, str]
) -> None:
    ids = [_ready(db_path, users[0], now=NOW + timedelta(hours=n)) for n in range(4)]
    other = _ready(db_path, users[1], now=NOW)
    expired = store.expire_oldest_ready(users[0], keep=2, now=NOW, db_path=db_path)
    assert {job.id for job in expired} == {ids[0], ids[1]}
    statuses = {i: store.get_job(users[0], i, db_path=db_path).status for i in ids}
    assert statuses == {
        ids[0]: store.EXPIRED,
        ids[1]: store.EXPIRED,
        ids[2]: store.READY,
        ids[3]: store.READY,
    }
    assert store.get_job(users[1], other, db_path=db_path).status == store.READY


def test_expire_job_requires_ownership_and_a_ready_export(
    db_path: str, users: tuple[str, str]
) -> None:
    job_id = _ready(db_path, users[0])
    assert not store.expire_job(users[1], job_id, now=NOW, db_path=db_path)
    assert store.expire_job(users[0], job_id, now=NOW, db_path=db_path)
    assert not store.expire_job(users[0], job_id, now=NOW, db_path=db_path)


def test_reap_stale_fails_jobs_whose_lease_lapsed(
    db_path: str, users: tuple[str, str]
) -> None:
    running, _ = _create(db_path, users[0])
    store.claim_job(running.id, now=NOW, db_path=db_path)
    queued, _ = _create(db_path, users[1])
    lease = {"running_lease": timedelta(minutes=2), "queued_lease": timedelta(minutes=15)}

    assert store.reap_stale(NOW + timedelta(minutes=1), db_path=db_path, **lease) == []
    reaped = store.reap_stale(NOW + timedelta(minutes=3), db_path=db_path, **lease)
    assert [job.id for job in reaped] == [running.id]
    failed = store.get_job(users[0], running.id, db_path=db_path)
    assert (failed.status, failed.error_code) == (store.FAILED, "interrupted")
    assert store.get_job(users[1], queued.id, db_path=db_path).status == store.QUEUED

    reaped = store.reap_stale(NOW + timedelta(minutes=16), db_path=db_path, **lease)
    assert [job.id for job in reaped] == [queued.id]
    assert (store.EVENT_FAILED, store.OUTCOME_FAILURE, queued.id) in _events(db_path, users[1])


def test_a_heartbeat_keeps_a_running_job_alive(
    db_path: str, users: tuple[str, str]
) -> None:
    job, _ = _create(db_path, users[0])
    store.claim_job(job.id, now=NOW, db_path=db_path)
    store.touch_job(job.id, now=NOW + timedelta(minutes=5), db_path=db_path)
    reaped = store.reap_stale(
        NOW + timedelta(minutes=6),
        running_lease=timedelta(minutes=2),
        queued_lease=timedelta(minutes=15),
        db_path=db_path,
    )
    assert reaped == []


def test_reap_stale_can_be_limited_to_one_owner(
    db_path: str, users: tuple[str, str]
) -> None:
    mine, _ = _create(db_path, users[0])
    theirs, _ = _create(db_path, users[1])
    later = NOW + timedelta(hours=1)
    lease = {"running_lease": timedelta(minutes=2), "queued_lease": timedelta(minutes=15)}
    reaped = store.reap_stale(later, user_id=users[0], db_path=db_path, **lease)
    assert [job.id for job in reaped] == [mine.id]
    assert store.get_job(users[1], theirs.id, db_path=db_path).status == store.QUEUED


def test_prune_terminal_removes_old_finished_rows_but_keeps_audit(
    db_path: str, users: tuple[str, str]
) -> None:
    old = _ready(db_path, users[0], now=NOW - timedelta(days=40))
    store.expire_job(users[0], old, now=NOW, db_path=db_path)
    recent_ready = _ready(db_path, users[0], now=NOW)
    assert store.prune_terminal(NOW - timedelta(days=30), db_path=db_path) == 1
    assert store.get_job(users[0], old, db_path=db_path) is None
    assert store.get_job(users[0], recent_ready, db_path=db_path) is not None
    assert any(event[2] == old for event in _events(db_path, users[0]))


def test_live_export_ids_lists_only_jobs_that_may_have_artifacts(
    db_path: str, users: tuple[str, str]
) -> None:
    ready = _ready(db_path, users[0])
    queued, _ = _create(db_path, users[1])
    gone = _ready(db_path, users[0], now=NOW + timedelta(minutes=1))
    store.delete_job(users[0], gone, now=NOW, db_path=db_path)
    assert store.live_export_ids(db_path=db_path) == {ready, queued.id}
