"""Tests for the deterministic security alert rules."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import security_alerts as alerts
import security_audit as audit
from database import init_db
from security_audit import (
    ActorType,
    AuditEventType,
    AuditOutcome,
    AuditSeverity,
    AuditSubjectType,
    SecurityEvent,
    resolve_audit_key,
    verify_chain,
)

_KEY = resolve_audit_key({"AUDIT_LOG_HMAC_KEY": "k" * 40})
_T0 = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "alerts.db")
    init_db(path)
    return path


def _alert_rows(db: str) -> list[dict[str, Any]]:
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM security_audit_events WHERE event_type LIKE 'alert.%' ORDER BY seq"
            )
        ]


def _failed_login(
    db: str,
    offset: timedelta,
    *,
    source: str = "src-a",
    identity: str | None = None,
    outcome: AuditOutcome = AuditOutcome.FAILURE,
) -> audit.AuditRecord | None:
    return alerts.record_and_evaluate(
        db,
        SecurityEvent(
            event_type=AuditEventType.LOGIN,
            outcome=outcome,
            actor_type=ActorType.ANONYMOUS,
            subject_type=AuditSubjectType.IDENTITY,
            subject_id=identity,
            request_id=f"req-{int(offset.total_seconds())}",
            source_ref=source,
            occurred_at=_T0 + offset,
            metadata={"reason": "oauth_exchange_failed"},
        ),
        key=_KEY,
    )


def _user_event(
    db: str,
    event_type: AuditEventType,
    offset: timedelta,
    *,
    user: str = "user-a",
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
) -> audit.AuditRecord | None:
    return alerts.record_and_evaluate(
        db,
        SecurityEvent(
            event_type=event_type,
            outcome=outcome,
            actor_type=ActorType.USER,
            actor_id=user,
            user_id=user,
            subject_type=AuditSubjectType.CONNECTOR,
            subject_id="hevy",
            request_id=f"req-{user}-{int(offset.total_seconds())}",
            source_ref="src-a",
            occurred_at=_T0 + offset,
        ),
        key=_KEY,
    )


# -- repeated authentication failures -------------------------------------------


def test_repeated_failed_logins_from_one_source_raise_one_warning(
    db: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="security_alerts"):
        for index in range(4):
            _failed_login(db, timedelta(seconds=index))
        assert _alert_rows(db) == []
        _failed_login(db, timedelta(seconds=4))
        _failed_login(db, timedelta(seconds=5))  # a sixth failure must not re-alert

    (alert,) = _alert_rows(db)
    assert alert["event_type"] == "alert.repeated_auth_failures"
    assert (alert["outcome"], alert["severity"]) == ("raised", "warning")
    assert (alert["actor_type"], alert["actor_id"], alert["user_id"]) == (
        "system",
        None,
        None,
    )
    assert (alert["subject_type"], alert["subject_id"]) == ("source", "src-a")
    assert alert["request_id"] == "req-4"  # correlated with the triggering request
    metadata = audit.json.loads(alert["metadata_json"])
    assert metadata == {
        "rule": "failed_login_burst_by_source",
        "scope": "source",
        "count": 5,
        "threshold": 5,
        "window_seconds": 600,
        "trigger_seq": 5,
    }
    assert (
        "security_alert alert_type=alert.repeated_auth_failures severity=warning"
        in caplog.text
    )


def test_failures_spread_over_time_or_sources_do_not_alert(db: str) -> None:
    for index in range(5):  # one failure every 3 minutes: never 5 inside 10 minutes
        _failed_login(db, timedelta(minutes=3 * index))
    for index in range(4):  # 4 + 4 from two different sources
        _failed_login(db, timedelta(hours=1, seconds=index), source="src-b")
        _failed_login(db, timedelta(hours=1, seconds=index), source="src-c")

    assert _alert_rows(db) == []


def test_denied_logins_count_and_a_new_alert_follows_the_cooldown(db: str) -> None:
    for index in range(5):
        _failed_login(db, timedelta(seconds=index), outcome=AuditOutcome.DENIED)
    assert len(_alert_rows(db)) == 1

    for index in range(5):  # inside the 30 minute cooldown
        _failed_login(db, timedelta(minutes=20, seconds=index))
    assert len(_alert_rows(db)) == 1

    for index in range(5):  # after the cooldown the attack is reported again
        _failed_login(db, timedelta(minutes=45, seconds=index))
    assert len(_alert_rows(db)) == 2
    assert verify_chain(db, key=_KEY).ok


def test_one_identity_targeted_from_many_sources_raises_an_identity_alert(
    db: str,
) -> None:
    for index in range(5):
        _failed_login(
            db, timedelta(seconds=index), source=f"src-{index}", identity="identity-ref"
        )

    (alert,) = _alert_rows(db)
    assert (alert["subject_type"], alert["subject_id"]) == ("identity", "identity-ref")
    assert (
        audit.json.loads(alert["metadata_json"])["rule"]
        == "failed_login_burst_by_identity"
    )


# -- unusual export activity ----------------------------------------------------


def test_export_burst_alerts_per_account_and_ignores_other_tenants(db: str) -> None:
    for index in range(2):
        _user_event(db, AuditEventType.DATA_EXPORTED, timedelta(minutes=index))
        _user_event(
            db, AuditEventType.DATA_EXPORTED, timedelta(minutes=index), user="user-b"
        )
    assert _alert_rows(db) == []

    _user_event(db, AuditEventType.DATA_EXPORTED, timedelta(minutes=3))

    (alert,) = _alert_rows(db)
    assert alert["event_type"] == "alert.unusual_export_activity"
    assert (alert["subject_type"], alert["subject_id"], alert["user_id"]) == (
        "account",
        "user-a",
        "user-a",
    )
    assert alert["severity"] == "warning"


def test_a_denied_export_is_critical_immediately(db: str) -> None:
    _user_event(
        db, AuditEventType.DATA_EXPORTED, timedelta(0), outcome=AuditOutcome.DENIED
    )

    (alert,) = _alert_rows(db)
    assert alert["event_type"] == "alert.unusual_export_activity"
    assert alert["severity"] == "critical"
    assert audit.json.loads(alert["metadata_json"])["rule"] == "export_denied"


# -- connector abuse ------------------------------------------------------------


def test_connector_failure_burst_alerts_only_the_affected_account(db: str) -> None:
    for index in range(4):
        _user_event(
            db,
            AuditEventType.CONNECTOR_TEST,
            timedelta(seconds=index),
            outcome=AuditOutcome.FAILURE,
        )
        _user_event(
            db,
            AuditEventType.CONNECTOR_SYNC,
            timedelta(seconds=index),
            user="user-b",
            outcome=AuditOutcome.SUCCESS,
        )
    assert _alert_rows(db) == []

    _user_event(
        db,
        AuditEventType.CONNECTOR_SYNC,
        timedelta(seconds=5),
        outcome=AuditOutcome.FAILURE,
    )

    (alert,) = _alert_rows(db)
    assert alert["event_type"] == "alert.connector_abuse"
    assert alert["user_id"] == "user-a"
    assert audit.json.loads(alert["metadata_json"])["rule"] == "connector_failure_burst"


def test_connector_connect_disconnect_churn_alerts(db: str) -> None:
    for index in range(4):
        _user_event(
            db, AuditEventType.CONNECTOR_CONNECTED, timedelta(seconds=2 * index)
        )
        _user_event(
            db, AuditEventType.CONNECTOR_DISCONNECTED, timedelta(seconds=2 * index + 1)
        )

    (alert,) = _alert_rows(db)
    assert alert["event_type"] == "alert.connector_abuse"
    assert audit.json.loads(alert["metadata_json"])["rule"] == "connector_change_churn"


# -- disabled production authentication -----------------------------------------


@pytest.mark.parametrize(
    ("environment", "authentication", "anonymous", "expected"),
    [
        ("production", False, False, True),
        ("production", False, True, True),
        ("production", True, True, True),
        ("PROD", False, False, True),
        ("production", True, False, False),
        ("development", False, True, False),
        ("test", False, True, False),
        ("", False, True, False),
    ],
)
def test_production_auth_disabled_rule_matrix(
    db: str, environment: str, authentication: bool, anonymous: bool, expected: bool
) -> None:
    alert = alerts.evaluate_runtime_posture(
        db,
        environment=environment,
        authentication_enabled=authentication,
        anonymous_enabled=anonymous,
        detected_by="startup",
        key=_KEY,
    )

    assert (alert is not None) is expected
    assert len(_alert_rows(db)) == int(expected)
    if alert is not None:
        assert alert.event_type == "alert.production_auth_disabled"
        assert alert.severity == "critical"
        assert (alert.subject_type, alert.subject_id) == ("system", "web")
        assert alert.metadata == {
            "rule": "production_auth_disabled",
            "environment": "production",
            "authentication_enabled": authentication,
            "anonymous_enabled": anonymous,
            "detected_by": "startup",
        }


def test_production_auth_disabled_alert_is_deduplicated_and_logged_critical(
    db: str, caplog: pytest.LogCaptureFixture
) -> None:
    kwargs: dict[str, Any] = {
        "environment": "production",
        "authentication_enabled": False,
        "anonymous_enabled": True,
        "key": _KEY,
    }
    with caplog.at_level(logging.CRITICAL, logger="security_alerts"):
        first = alerts.evaluate_runtime_posture(db, detected_by="startup", **kwargs)
        second = alerts.evaluate_runtime_posture(
            db, detected_by="anonymous_request", **kwargs
        )

    assert first is not None and second is None
    assert len(_alert_rows(db)) == 1
    assert (
        caplog.text.count("security_alert alert_type=alert.production_auth_disabled")
        == 1
    )
    assert any(record.levelno == logging.CRITICAL for record in caplog.records)


def test_production_environment_names_match_the_web_runtime_contract() -> None:
    from webapp.runtime_security import _PRODUCTION_ENVIRONMENTS

    assert alerts.PRODUCTION_ENVIRONMENTS == _PRODUCTION_ENVIRONMENTS


# -- engine behaviour -----------------------------------------------------------


def test_alert_events_do_not_retrigger_rules_and_the_chain_stays_valid(db: str) -> None:
    for index in range(5):
        _failed_login(db, timedelta(seconds=index))

    with sqlite3.connect(db) as conn:
        types = [
            row[0]
            for row in conn.execute("SELECT event_type FROM security_audit_events")
        ]
    assert (
        types.count("auth.login") == 5
        and types.count("alert.repeated_auth_failures") == 1
    )
    assert verify_chain(db, key=_KEY).ok


def test_rule_evaluation_failure_never_blocks_recording(
    db: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(alerts, "_count_matching", broken)

    with caplog.at_level(logging.ERROR, logger="security_alerts"):
        record = _failed_login(db, timedelta(0))

    assert record is not None and record.seq == 1
    assert (
        "security_alert_evaluation_failed rule=failed_login_burst_by_source"
        in caplog.text
    )


def test_events_without_a_scope_value_cannot_trigger_a_scoped_rule(db: str) -> None:
    for index in range(6):
        alerts.record_and_evaluate(
            db,
            SecurityEvent(
                event_type=AuditEventType.LOGIN,
                outcome=AuditOutcome.FAILURE,
                actor_type=ActorType.ANONYMOUS,
                subject_type=AuditSubjectType.IDENTITY,
                occurred_at=_T0 + timedelta(seconds=index),
            ),
            key=_KEY,
        )

    assert _alert_rows(db) == []


def test_every_default_rule_alerts_with_an_audit_event_type_and_known_severity() -> (
    None
):
    for rule in alerts.DEFAULT_RULES:
        assert rule.alert_type in audit.ALERT_EVENT_TYPES
        assert rule.threshold >= 1 and rule.window > timedelta(0)
        assert isinstance(rule.severity, AuditSeverity)
        assert not rule.trigger_types & audit.ALERT_EVENT_TYPES
    assert {
        rule.alert_type for rule in alerts.DEFAULT_RULES
    } == audit.ALERT_EVENT_TYPES - {AuditEventType.ALERT_PRODUCTION_AUTH_DISABLED}
