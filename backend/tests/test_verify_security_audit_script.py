"""Tests for the operator-facing audit chain verification script."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
import security_audit as audit
from database import init_db
from scripts.verify_security_audit import main
from security_audit import (
    ActorType,
    AuditEventType,
    AuditOutcome,
    AuditSubjectType,
    SecurityEvent,
    record_security_event,
    resolve_audit_key,
)

_ENV_KEY = "k" * 40


@pytest.fixture(autouse=True)
def _audit_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_LOG_HMAC_KEY", _ENV_KEY)
    monkeypatch.delenv("WEB_AUTH_SECRET", raising=False)


@pytest.fixture()
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "verify.db")
    init_db(path)
    key = resolve_audit_key()
    for event_type, outcome in (
        (AuditEventType.LOGIN, AuditOutcome.SUCCESS),
        (AuditEventType.CREDENTIAL_SAVED, AuditOutcome.SUCCESS),
        (AuditEventType.ALERT_CONNECTOR_ABUSE, AuditOutcome.RAISED),
    ):
        is_alert = outcome is AuditOutcome.RAISED
        record_security_event(
            path,
            SecurityEvent(
                event_type=event_type,
                outcome=outcome,
                actor_type=ActorType.SYSTEM if is_alert else ActorType.USER,
                actor_id=None if is_alert else "user-a",
                user_id="user-a",
                subject_type=AuditSubjectType.ACCOUNT,
                subject_id="user-a",
                occurred_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
                metadata={"rule": "synthetic"} if is_alert else {},
            ),
            key=key,
        )
    return path


def test_an_intact_chain_exits_zero_and_reports_head_alerts_and_counts(
    db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--db", db]) == 0

    output = capsys.readouterr().out
    assert "Security audit chain: OK (3 events, head seq 3" in output
    assert "Keyed digest: yes" in output
    assert "auth.login: 1" in output and "alert.connector_abuse: 1" in output
    assert "alert.connector_abuse" in output.split("Recent alerts:")[1]
    assert "user-a" not in output  # operator output stays pseudonymous


def test_json_output_is_machine_readable(
    db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--db", db, "--json", "--alerts", "1"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True and report["passed"] is True
    assert report["fully_authenticated"] is True and report["policy_failures"] == []
    assert report["checked"] == 3 and report["keyed"] is True and report["unkeyed"] == 0
    assert report["key_id"] == resolve_audit_key().key_id
    assert report["events_by_type"]["auth.login"] == 1
    assert [alert["event_type"] for alert in report["recent_alerts"]] == [
        "alert.connector_abuse"
    ]
    assert report["failure_reason"] is None


def test_tampering_exits_one_and_names_the_failing_row(
    db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER trg_security_audit_events_no_update")
        conn.execute(
            "UPDATE security_audit_events SET outcome = 'failure' WHERE seq = 2"
        )

    assert main(["--db", db]) == 1

    assert "FAILED at seq 2 (hash_mismatch)" in capsys.readouterr().out


def test_rows_the_verifier_cannot_authenticate_fail_unless_explicitly_allowed(
    db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AUDIT_LOG_HMAC_KEY", "x" * 40)  # a different key

    assert main(["--db", db]) == 1
    output = capsys.readouterr().out
    assert "NOT FULLY AUTHENTICATED" in output
    assert "FAILED: 3 rows were signed with a key that is not configured" in output
    assert "link-checked only): 3" in output

    assert main(["--db", db, "--allow-unverifiable"]) == 0  # deliberate rotation


def test_require_keyed_fails_when_no_key_material_is_configured(
    db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AUDIT_LOG_HMAC_KEY")

    assert main(["--db", db, "--require-keyed", "--allow-unverifiable"]) == 1
    assert "FAILED: key material is required" in capsys.readouterr().out
    assert (
        main(["--db", db, "--allow-unverifiable"]) == 0
    )  # tolerated when not required


def test_require_keyed_rejects_a_chain_that_contains_unkeyed_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = str(tmp_path / "unkeyed.db")
    init_db(path)
    for _ in range(2):
        record_security_event(
            path,
            SecurityEvent(
                event_type=AuditEventType.LOGIN,
                outcome=AuditOutcome.SUCCESS,
                actor_type=ActorType.USER,
                actor_id="user-a",
                user_id="user-a",
                subject_type=AuditSubjectType.ACCOUNT,
            ),
            key=audit.UNKEYED,
        )

    assert main(["--db", path]) == 0  # internally consistent
    capsys.readouterr()

    assert main(["--db", path, "--require-keyed", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is False and report["unkeyed"] == 2
    assert report["policy_failures"] == [
        "2 rows are unkeyed; a keyed chain must contain none"
    ]


def test_unusable_inputs_exit_two_without_leaking_detail(
    tmp_path: Path,
    db: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--db", str(tmp_path / "absent.db")]) == 2
    assert "database does not exist" in capsys.readouterr().err

    monkeypatch.setenv("AUDIT_LOG_HMAC_KEY", "short")
    assert main(["--db", db]) == 2
    err = capsys.readouterr().err
    assert "SecurityAuditConfigurationError" in err and "short" not in err


def test_the_verifier_agrees_with_the_library_on_the_same_database(db: str) -> None:
    assert audit.verify_chain(db, key=resolve_audit_key()).ok
