"""Tests for the tamper-evident, tenant-aware security audit log."""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import security_audit as audit
from database import init_db
from security_audit import (
    GENESIS_HASH,
    ActorType,
    AuditEventType,
    AuditKey,
    AuditOutcome,
    AuditPolicy,
    AuditSeverity,
    AuditSubjectType,
    SecurityAuditConfigurationError,
    SecurityEvent,
    record_security_event,
    resolve_audit_key,
    sanitise_metadata,
    verify_chain,
)

_KEY = resolve_audit_key({"AUDIT_LOG_HMAC_KEY": "k" * 40})
_T0 = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "audit.db")
    init_db(path)
    return path


def _login(**overrides: Any) -> SecurityEvent:
    fields: dict[str, Any] = {
        "event_type": AuditEventType.LOGIN,
        "outcome": AuditOutcome.SUCCESS,
        "actor_type": ActorType.USER,
        "actor_id": "user-a",
        "user_id": "user-a",
        "subject_type": AuditSubjectType.ACCOUNT,
        "subject_id": "user-a",
        "request_id": "req-1",
        "source_ref": "src-1",
        "metadata": {"method": "google_oauth"},
        "occurred_at": _T0,
    }
    fields.update(overrides)
    return SecurityEvent(**fields)


def _record(
    db: str, event: SecurityEvent | None = None, **kwargs: Any
) -> audit.AuditRecord:
    record = record_security_event(db, event or _login(), key=_KEY, **kwargs)
    assert record is not None
    return record


def _rows(db: str) -> list[dict[str, Any]]:
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute("SELECT * FROM security_audit_events ORDER BY seq")
        ]


def _as_attacker(db: str) -> sqlite3.Connection:
    """Open the database the way someone with raw write access could: no triggers."""

    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER IF EXISTS trg_security_audit_events_no_update")
    conn.execute("DROP TRIGGER IF EXISTS trg_security_audit_events_no_delete")
    return conn


# -- schema and tenancy ---------------------------------------------------------


def test_init_db_creates_tenant_indexed_append_only_table_idempotently(db: str) -> None:
    init_db(db)  # a second initialisation must be a no-op

    with sqlite3.connect(db) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(security_audit_events)")
        }
        assert {
            "user_id",
            "actor_id",
            "request_id",
            "outcome",
            "prev_hash",
            "event_hash",
        } <= columns
        index_columns = [
            conn.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (name,)
            ).fetchall()
            for (name,) in conn.execute(
                "SELECT name FROM pragma_index_list('security_audit_events')"
            )
        ]
        assert any(cols and cols[0][0] == "user_id" for cols in index_columns)

    _record(db)
    with sqlite3.connect(db) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE security_audit_events SET user_id = 'user-b'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM security_audit_events")


def test_recording_creates_the_table_for_a_database_not_built_by_init_db(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "bare.db")

    assert record_security_event(path, _login(), key=_KEY) is not None
    assert verify_chain(path, key=_KEY).ok


# -- recorded fields ------------------------------------------------------------


def test_event_records_actor_subject_time_correlation_outcome_and_metadata(
    db: str,
) -> None:
    record = _record(
        db,
        _login(
            outcome=AuditOutcome.DENIED,
            actor_type=ActorType.ANONYMOUS,
            actor_id=None,
            user_id=None,
            subject_type=AuditSubjectType.IDENTITY,
            subject_id="identity-ref",
            request_id="0123456789abcdef0123456789abcdef",
            source_ref="source-ref",
            metadata={"reason": "email_not_allowed"},
        ),
    )

    (row,) = _rows(db)
    assert row["seq"] == record.seq == 1
    assert row["event_type"] == "auth.login"
    assert row["outcome"] == "denied"
    assert row["severity"] == "warning"  # derived from a non-success outcome
    assert (row["actor_type"], row["actor_id"], row["user_id"]) == (
        "anonymous",
        None,
        None,
    )
    assert (row["subject_type"], row["subject_id"]) == ("identity", "identity-ref")
    assert row["request_id"] == "0123456789abcdef0123456789abcdef"
    assert row["source_ref"] == "source-ref"
    assert row["metadata_json"] == '{"reason":"email_not_allowed"}'
    assert datetime.fromisoformat(row["occurred_at"]) == _T0
    assert row["occurred_at"].endswith("+00:00")
    assert row["key_id"] == _KEY.key_id


def test_success_events_default_to_info_and_explicit_severity_is_kept(db: str) -> None:
    assert _record(db).severity == "info"
    assert _record(db, _login(severity=AuditSeverity.CRITICAL)).severity == "critical"


def test_naive_timestamps_and_inconsistent_actors_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _login(occurred_at=datetime(2030, 1, 1))  # noqa: DTZ001 - naive on purpose
    with pytest.raises(ValueError, match="require an actor_id"):
        _login(actor_id=None)
    with pytest.raises(ValueError, match="cannot carry an actor_id"):
        _login(actor_type=ActorType.ANONYMOUS)
    with pytest.raises(ValueError, match="cannot carry an actor_id"):
        _login(actor_type=ActorType.SYSTEM)


def test_indexed_identifier_columns_accept_only_compact_safe_values(db: str) -> None:
    record = _record(
        db,
        _login(
            actor_id="someone@example.com",
            subject_id="x" * 200,
            request_id="a b",
            source_ref=" ",
        ),
    )

    assert record.actor_id == "[invalid]"
    assert record.subject_id == "[invalid]"
    assert record.request_id == "[invalid]"
    assert record.source_ref is None


# -- safe metadata --------------------------------------------------------------


_SYNTHETIC_SECRETS = (
    "sk-synthetic-secret-value-0123456789",
    "Bearer synthetic-bearer-credential",
    (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJzeW50aGV0aWMiOjF9."
        "c3ludGhldGljLXNpZw"
    ),
    "0f9d8c7b-6a5e-4d3c-8b2a-1f0e9d8c7b6a",
    "synthetic.person@example.com",
    "https://example.com/callback?code=synthetic-oauth-code&state=synthetic-state",
    "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
)


def test_metadata_is_redacted_flat_bounded_and_secret_free() -> None:
    metadata = sanitise_metadata(
        {
            "api_key": _SYNTHETIC_SECRETS[0],
            "refresh_token": "synthetic-refresh-token",
            "email": "synthetic.person@example.com",
            "note_bearer": _SYNTHETIC_SECRETS[1],
            "note_jwt": _SYNTHETIC_SECRETS[2],
            "note_uuid": _SYNTHETIC_SECRETS[3],
            "note_email": f"contact {_SYNTHETIC_SECRETS[4]} today",
            "note_url": _SYNTHETIC_SECRETS[5],
            "note_opaque": _SYNTHETIC_SECRETS[6],
            "Bad Key!": "kept",
            "nested": {"api_key": "synthetic"},
            "blob": b"bytes",
            "values": ["a", {"x": 1}, 3],
            "ratio": float("nan"),
            "count": 3,
            "ok": True,
            "reason": "word " * 100,
            "ctrl": "line\nbreak\x00",
        }
    )

    flat = repr(metadata)
    for secret in (*_SYNTHETIC_SECRETS, "synthetic-refresh-token"):
        assert secret not in flat
    assert metadata["api_key"] == "[REDACTED:CREDENTIAL]"
    assert metadata["refresh_token"] == "[REDACTED:CREDENTIAL]"
    assert metadata["email"] == "[REDACTED:IDENTIFIER]"
    assert metadata["bad_key_"] == "kept"
    assert metadata["nested"] == "[UNSUPPORTED]"
    assert metadata["blob"] == "[UNSUPPORTED]"
    assert metadata["values"] == ["a", "[UNSUPPORTED]", 3]
    assert metadata["ratio"] is None
    assert (metadata["count"], metadata["ok"]) == (3, True)
    assert len(metadata["reason"]) == 200  # long free text is truncated
    assert metadata["ctrl"] == "line break "


def test_metadata_is_capped_in_item_count_and_total_size() -> None:
    many = sanitise_metadata({f"key_{index:03d}": index for index in range(100)})
    assert len(many) == 24

    huge = sanitise_metadata(
        {f"key_{index:02d}": ["x" * 100] * 20 for index in range(24)}
    )
    assert huge == {"truncated": True}
    assert sanitise_metadata(None) == {}
    assert sanitise_metadata({}) == {}


def test_secrets_passed_as_metadata_never_reach_the_database(db: str) -> None:
    _record(
        db,
        _login(
            metadata={
                "api_key": _SYNTHETIC_SECRETS[0],
                "detail": f"failed with {_SYNTHETIC_SECRETS[1]} and {_SYNTHETIC_SECRETS[5]}",
            }
        ),
    )

    stored = repr(_rows(db))
    for secret in _SYNTHETIC_SECRETS:
        assert secret not in stored


# -- keys and pseudonyms --------------------------------------------------------


def test_pseudonyms_are_stable_keyed_and_do_not_contain_the_value() -> None:
    other = resolve_audit_key({"AUDIT_LOG_HMAC_KEY": "z" * 40})

    first = _KEY.pseudonym("ip", "203.0.113.9")
    assert first == _KEY.pseudonym("ip", "203.0.113.9")
    assert first != _KEY.pseudonym("identity", "203.0.113.9")
    assert first != other.pseudonym("ip", "203.0.113.9")
    assert len(first) == 24 and "203" not in first
    assert audit.UNKEYED.pseudonym("ip", "203.0.113.9") != first


def test_key_resolution_prefers_dedicated_key_then_session_secret_then_unkeyed() -> (
    None
):
    dedicated = resolve_audit_key(
        {"AUDIT_LOG_HMAC_KEY": "d" * 40, "WEB_AUTH_SECRET": "s" * 40}
    )
    derived = resolve_audit_key({"WEB_AUTH_SECRET": "s" * 40})
    weak_session = resolve_audit_key({"WEB_AUTH_SECRET": "short"})
    unkeyed = resolve_audit_key({})

    assert dedicated.keyed and derived.keyed and dedicated.key_id != derived.key_id
    assert derived == resolve_audit_key({"WEB_AUTH_SECRET": "s" * 40})
    assert resolve_audit_key({"WEB_AUTH_SECRET": "t" * 40}).key_id != derived.key_id
    assert not weak_session.keyed and not unkeyed.keyed
    assert unkeyed.key_id == "unkeyed"
    for key in (dedicated, derived):
        assert "d" * 12 not in key.key_id and "s" * 12 not in key.key_id


def test_a_short_dedicated_key_is_rejected_instead_of_silently_weakening_the_chain(
    db: str,
) -> None:
    with pytest.raises(SecurityAuditConfigurationError, match="at least 32"):
        resolve_audit_key({"AUDIT_LOG_HMAC_KEY": "too-short"})


# -- hash chain and tamper evidence ---------------------------------------------


def test_events_form_a_verifiable_hash_chain(db: str) -> None:
    first = _record(db)
    second = _record(db, _login(outcome=AuditOutcome.FAILURE))
    third = _record(db, _login(event_type=AuditEventType.SESSION_REVOKED))

    assert first.prev_hash == GENESIS_HASH
    assert second.prev_hash == first.event_hash
    assert third.prev_hash == second.event_hash
    assert len({first.event_hash, second.event_hash, third.event_hash}) == 3

    result = verify_chain(db, key=_KEY)
    assert result.ok and result.keyed
    assert (result.checked, result.head_seq, result.head_hash) == (
        3,
        3,
        third.event_hash,
    )
    assert result.unverifiable == 0


def test_an_empty_or_missing_audit_table_verifies_trivially(tmp_path: Path) -> None:
    path = tmp_path / "plain.db"
    sqlite3.connect(path).close()
    assert verify_chain(str(path), key=_KEY).ok
    with pytest.raises(FileNotFoundError):
        verify_chain(str(tmp_path / "absent.db"), key=_KEY)


def test_editing_a_row_is_detected_at_that_row(db: str) -> None:
    for _ in range(3):
        _record(db)
    with _as_attacker(db) as conn:
        conn.execute(
            "UPDATE security_audit_events SET outcome = 'success', user_id = 'user-b' WHERE seq = 2"
        )

    result = verify_chain(db, key=_KEY)

    assert not result.ok
    assert (result.failure_seq, result.failure_reason) == (2, "hash_mismatch")
    assert result.checked == 1 and result.head_seq == 1


def test_removing_a_row_is_detected_whether_middle_head_or_tail(db: str) -> None:
    for _ in range(4):
        _record(db)
    with _as_attacker(db) as conn:
        conn.execute("DELETE FROM security_audit_events WHERE seq = 2")
    middle = verify_chain(db, key=_KEY)
    assert (middle.failure_seq, middle.failure_reason) == (3, "sequence_gap")

    with _as_attacker(db) as conn:
        conn.execute("DELETE FROM security_audit_events WHERE seq = 1")
    head = verify_chain(db, key=_KEY)
    assert (head.failure_seq, head.failure_reason) == (3, "sequence_gap")

    with _as_attacker(db) as conn:
        conn.execute("DELETE FROM security_audit_events WHERE seq = 4")
    with _as_attacker(db) as conn:
        conn.execute("DELETE FROM security_audit_events")
    emptied = verify_chain(db, key=_KEY)
    assert (emptied.failure_seq, emptied.failure_reason) == (1, "tail_truncated")


def test_truncating_the_tail_stays_detectable_after_new_events_are_appended(
    db: str,
) -> None:
    for _ in range(3):
        _record(db)
    with _as_attacker(db) as conn:
        conn.execute("DELETE FROM security_audit_events WHERE seq = 3")
    assert verify_chain(db, key=_KEY).failure_reason == "tail_truncated"

    # The sequence is never reused, so the gap cannot be papered over.
    assert _record(db).seq == 4
    result = verify_chain(db, key=_KEY)
    assert (result.failure_seq, result.failure_reason) == (4, "sequence_gap")


def test_rewriting_a_link_is_detected(db: str) -> None:
    for _ in range(3):
        _record(db)
    with _as_attacker(db) as conn:
        conn.execute(
            "UPDATE security_audit_events SET prev_hash = ? WHERE seq = 3",
            (GENESIS_HASH,),
        )

    result = verify_chain(db, key=_KEY)

    assert (result.failure_seq, result.failure_reason) == (3, "broken_link")


def test_recomputing_the_whole_chain_without_the_key_is_detected(db: str) -> None:
    for _ in range(3):
        _record(db)
    rows = _rows(db)
    forged_key = AuditKey(secret=None, key_id=_KEY.key_id)  # attacker lacks the secret
    with _as_attacker(db) as conn:
        previous = GENESIS_HASH
        for row in rows:
            row["prev_hash"] = previous
            row["outcome"] = "success"
            previous = audit.event_hash_for(forged_key, row)
            conn.execute(
                "UPDATE security_audit_events SET outcome = ?, prev_hash = ?, event_hash = ? "
                "WHERE seq = ?",
                ("success", row["prev_hash"], previous, row["seq"]),
            )

    result = verify_chain(db, key=_KEY)

    assert (result.failure_seq, result.failure_reason) == (1, "hash_mismatch")


def test_rows_signed_by_another_key_are_link_checked_but_never_fully_authenticated(
    db: str,
) -> None:
    rotated = resolve_audit_key({"AUDIT_LOG_HMAC_KEY": "r" * 40})
    record_security_event(db, _login(), key=rotated)
    record_security_event(db, _login(), key=rotated)
    _record(db)  # signed with the current key, linked to the rotated key's last row

    result = verify_chain(db, key=_KEY)
    assert result.ok and result.checked == 3 and result.unverifiable == 2
    assert not result.fully_authenticated  # a gate must not accept a claimed rotation
    assert (
        verify_chain(db, key=rotated).unverifiable == 1
    )  # each segment verifies with its key

    with _as_attacker(db) as conn:
        conn.execute("DELETE FROM security_audit_events WHERE seq = 2")
    assert verify_chain(db, key=_KEY).failure_reason == "sequence_gap"


def test_an_attacker_cannot_downgrade_a_keyed_chain_to_plain_sha256(db: str) -> None:
    for _ in range(4):
        _record(db)
    rows = _rows(db)
    forged = audit.UNKEYED
    with _as_attacker(db) as conn:
        previous = rows[0]["event_hash"]
        for row in rows[1:]:  # rewrite everything after the first row as "unkeyed"
            row.update(
                prev_hash=previous, key_id=audit.UNKEYED_KEY_ID, outcome="failure"
            )
            previous = audit.event_hash_for(forged, row)
            conn.execute(
                "UPDATE security_audit_events SET outcome = ?, key_id = ?, prev_hash = ?, "
                "event_hash = ? WHERE seq = ?",
                (
                    "failure",
                    audit.UNKEYED_KEY_ID,
                    row["prev_hash"],
                    previous,
                    row["seq"],
                ),
            )

    result = verify_chain(db, key=_KEY)

    assert not result.ok
    assert (result.failure_seq, result.failure_reason) == (2, "key_downgrade")


def test_a_wholesale_unkeyed_rewrite_is_visible_in_the_unkeyed_count(db: str) -> None:
    for _ in range(3):
        record_security_event(db, _login(), key=audit.UNKEYED)

    result = verify_chain(db, key=_KEY)

    assert (
        result.ok and result.unkeyed == 3
    )  # callers that require keyed chains reject this
    assert result.as_dict()["unkeyed"] == 3


def test_unkeyed_rows_still_chain_and_verify_without_any_secret(db: str) -> None:
    record_security_event(db, _login(), key=audit.UNKEYED)
    record_security_event(db, _login(), key=audit.UNKEYED)

    result = verify_chain(db, key=audit.UNKEYED)

    assert result.ok and not result.keyed and result.unverifiable == 0
    with _as_attacker(db) as conn:
        conn.execute(
            "UPDATE security_audit_events SET outcome = 'failure' WHERE seq = 1"
        )
    assert verify_chain(db, key=audit.UNKEYED).failure_reason == "hash_mismatch"


def test_concurrent_writers_extend_one_unbroken_chain(db: str) -> None:
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            for _ in range(10):
                assert record_security_event(db, _login(), key=_KEY) is not None
        except BaseException as exc:  # noqa: BLE001 - surface any thread failure
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert [row["seq"] for row in _rows(db)] == list(range(1, 81))
    assert verify_chain(db, key=_KEY).ok


# -- bounded growth and failure isolation ---------------------------------------


def test_anonymous_event_floods_are_bounded_per_source_and_type(db: str) -> None:
    policy = AuditPolicy(
        anonymous_events_per_window=3, anonymous_window=timedelta(minutes=10)
    )

    def attempt(source: str, offset: int, **overrides: Any) -> audit.AuditRecord | None:
        event = _login(
            outcome=AuditOutcome.FAILURE,
            actor_type=ActorType.ANONYMOUS,
            actor_id=None,
            user_id=None,
            source_ref=source,
            occurred_at=_T0 + timedelta(seconds=offset),
            **overrides,
        )
        return record_security_event(db, event, key=_KEY, policy=policy)

    assert [attempt("src-a", i) is not None for i in range(5)] == [
        True,
        True,
        True,
        False,
        False,
    ]
    assert attempt("src-b", 6) is not None  # another source has its own budget
    assert attempt("src-a", 7, event_type=AuditEventType.SESSION_REVOKED) is not None
    assert (
        record_security_event(
            db,
            _login(source_ref="src-a", occurred_at=_T0 + timedelta(seconds=8)),
            key=_KEY,
            policy=policy,
        )
        is not None
    )  # authenticated events are never dropped by the anonymous budget
    assert attempt("src-a", 11 * 60) is not None  # the window slides
    assert verify_chain(db, key=_KEY).ok


def test_recording_never_raises_when_storage_fails_and_logs_no_detail(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file")
    secret_event = _login(metadata={"api_key": "sk-synthetic-secret-value-0123456789"})

    with caplog.at_level(logging.ERROR, logger="security_audit"):
        result = record_security_event(
            str(blocker / "audit.db"), secret_event, key=_KEY
        )

    assert result is None
    assert (
        "security_audit_write_failed event_type=auth.login outcome=success"
        in caplog.text
    )
    assert "sk-synthetic" not in caplog.text


def test_recording_emits_an_anchor_log_line_without_identifiers(
    db: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="security_audit"):
        record = _record(db, _login(user_id="user-a", request_id="req-1"))

    assert f"seq={record.seq} event_type=auth.login outcome=success" in caplog.text
    assert f"event_hash={record.event_hash}" in caplog.text
    assert "request_id=req-1" in caplog.text
    assert "user-a" not in caplog.text


# -- operator reads -------------------------------------------------------------


def test_operator_reads_summarise_events_and_newest_alerts(db: str) -> None:
    _record(db)
    for index in range(3):
        _record(
            db,
            _login(
                event_type=AuditEventType.ALERT_CONNECTOR_ABUSE,
                outcome=AuditOutcome.RAISED,
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                subject_id=f"user-{index}",
                metadata={"rule": "r"},
            ),
        )

    assert audit.count_events(db) == {"alert.connector_abuse": 3, "auth.login": 1}
    alerts = list(audit.iter_alert_records(db, limit=2))
    assert [alert.seq for alert in alerts] == [4, 3]
    assert all(alert.event_type == "alert.connector_abuse" for alert in alerts)
