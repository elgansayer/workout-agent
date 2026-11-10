"""Deterministic anomaly alert rules over the security audit log.

Rules run synchronously after an audited event is stored. They are plain count
thresholds over a bounded time window so every alert is explainable and testable;
there is no model, sampling or hidden state. A fired rule is itself appended to
the tamper-evident chain as an ``alert.*`` event and logged with a stable
``security_alert`` marker that operators can route to their alerting stack.

Covered conditions
------------------
* ``repeated_auth_failures`` - failed or denied sign-ins from one network source
  or against one attempted identity.
* ``production_auth_disabled`` - a production-configured process that is running
  without complete authentication.
* ``unusual_export_activity`` - a burst of successful exports by one account, or
  any denied export attempt.
* ``connector_abuse`` - a burst of failed connector operations, or connector
  connect/disconnect churn, for one account.

Alert events are de-duplicated per rule, subject and severity by the store's
cooldown, so a sustained attack produces one row per cooldown window instead of
one per request.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

import security_audit as audit
from security_audit import (
    ActorType,
    AuditEventType,
    AuditKey,
    AuditOutcome,
    AuditPolicy,
    AuditRecord,
    AuditSeverity,
    AuditSubjectType,
    SecurityEvent,
)

logger = logging.getLogger(__name__)

# Mirrors webapp.runtime_security; a test keeps the two in sync. Importing the
# webapp package here would run its import-time runtime validation.
PRODUCTION_ENVIRONMENTS = frozenset({"prod", "production"})


class RuleScope(StrEnum):
    """What a threshold rule counts events per."""

    SOURCE = "source"
    IDENTITY = "identity"
    USER = "user"


# Fixed SQL fragments, selected by scope. No caller-supplied text reaches SQL.
_SCOPE_SQL = {
    RuleScope.SOURCE: "AND source_ref = ?",
    RuleScope.IDENTITY: "AND subject_type = 'identity' AND subject_id = ?",
    RuleScope.USER: "AND user_id = ?",
}


@dataclass(frozen=True, slots=True)
class ThresholdRule:
    """Fire ``alert_type`` when ``threshold`` matching events fall within ``window``."""

    name: str
    alert_type: AuditEventType
    trigger_types: frozenset[AuditEventType]
    outcomes: frozenset[AuditOutcome]
    scope: RuleScope
    window: timedelta
    threshold: int
    severity: AuditSeverity


_FAILED = frozenset({AuditOutcome.FAILURE, AuditOutcome.DENIED})

DEFAULT_RULES: tuple[ThresholdRule, ...] = (
    ThresholdRule(
        name="failed_login_burst_by_source",
        alert_type=AuditEventType.ALERT_REPEATED_AUTH_FAILURES,
        trigger_types=frozenset({AuditEventType.LOGIN}),
        outcomes=_FAILED,
        scope=RuleScope.SOURCE,
        window=timedelta(minutes=10),
        threshold=5,
        severity=AuditSeverity.WARNING,
    ),
    ThresholdRule(
        name="failed_login_burst_by_identity",
        alert_type=AuditEventType.ALERT_REPEATED_AUTH_FAILURES,
        trigger_types=frozenset({AuditEventType.LOGIN}),
        outcomes=_FAILED,
        scope=RuleScope.IDENTITY,
        window=timedelta(minutes=10),
        threshold=5,
        severity=AuditSeverity.WARNING,
    ),
    ThresholdRule(
        name="export_burst",
        alert_type=AuditEventType.ALERT_UNUSUAL_EXPORT_ACTIVITY,
        trigger_types=frozenset({AuditEventType.DATA_EXPORTED}),
        outcomes=frozenset({AuditOutcome.SUCCESS}),
        scope=RuleScope.USER,
        window=timedelta(hours=1),
        threshold=3,
        severity=AuditSeverity.WARNING,
    ),
    ThresholdRule(
        name="export_denied",
        alert_type=AuditEventType.ALERT_UNUSUAL_EXPORT_ACTIVITY,
        trigger_types=frozenset({AuditEventType.DATA_EXPORTED}),
        outcomes=frozenset({AuditOutcome.DENIED}),
        scope=RuleScope.USER,
        window=timedelta(hours=1),
        threshold=1,
        severity=AuditSeverity.CRITICAL,
    ),
    ThresholdRule(
        name="connector_failure_burst",
        alert_type=AuditEventType.ALERT_CONNECTOR_ABUSE,
        trigger_types=frozenset(
            {
                AuditEventType.CONNECTOR_CONNECTED,
                AuditEventType.CONNECTOR_SYNC,
                AuditEventType.CONNECTOR_TEST,
            }
        ),
        outcomes=_FAILED,
        scope=RuleScope.USER,
        window=timedelta(minutes=10),
        threshold=5,
        severity=AuditSeverity.WARNING,
    ),
    ThresholdRule(
        name="connector_change_churn",
        alert_type=AuditEventType.ALERT_CONNECTOR_ABUSE,
        trigger_types=frozenset(
            {AuditEventType.CONNECTOR_CONNECTED, AuditEventType.CONNECTOR_DISCONNECTED}
        ),
        outcomes=frozenset({AuditOutcome.SUCCESS}),
        scope=RuleScope.USER,
        window=timedelta(minutes=10),
        threshold=8,
        severity=AuditSeverity.WARNING,
    ),
)


def _scope_value(scope: RuleScope, record: AuditRecord) -> str | None:
    if scope is RuleScope.SOURCE:
        return record.source_ref
    if scope is RuleScope.IDENTITY:
        return (
            record.subject_id
            if record.subject_type == AuditSubjectType.IDENTITY
            else None
        )
    return record.user_id


def _alert_subject(scope: RuleScope) -> AuditSubjectType:
    return {
        RuleScope.SOURCE: AuditSubjectType.SOURCE,
        RuleScope.IDENTITY: AuditSubjectType.IDENTITY,
        RuleScope.USER: AuditSubjectType.ACCOUNT,
    }[scope]


def _count_matching(
    db_path: str, rule: ThresholdRule, scope_value: str, trigger: AuditRecord
) -> int:
    """Count events matching *rule* in the window ending at the triggering event."""

    trigger_types = sorted(item.value for item in rule.trigger_types)
    outcomes = sorted(item.value for item in rule.outcomes)
    window_start = (trigger.occurred - rule.window).isoformat(timespec="microseconds")
    query = (
        f"SELECT COUNT(*) FROM {audit.TABLE} "
        f"WHERE event_type IN ({', '.join('?' * len(trigger_types))}) "
        f"AND outcome IN ({', '.join('?' * len(outcomes))}) "
        f"{_SCOPE_SQL[rule.scope]} AND occurred_at >= ? AND occurred_at <= ?"
    )
    with audit.open_readonly(db_path) as conn:
        row = conn.execute(
            query,
            (
                *trigger_types,
                *outcomes,
                scope_value,
                window_start,
                trigger.occurred_at,
            ),
        ).fetchone()
    return int(row[0])


def _log_alert(alert: AuditRecord) -> None:
    level = (
        logging.CRITICAL
        if alert.severity == AuditSeverity.CRITICAL
        else logging.WARNING
    )
    logger.log(
        level,
        "security_alert alert_type=%s severity=%s subject_type=%s seq=%d request_id=%s",
        alert.event_type,
        alert.severity,
        alert.subject_type,
        alert.seq,
        alert.request_id,
    )


def evaluate_rules(
    db_path: str,
    record: AuditRecord,
    *,
    key: AuditKey | None = None,
    rules: Sequence[ThresholdRule] = DEFAULT_RULES,
    policy: AuditPolicy = audit.DEFAULT_POLICY,
) -> list[AuditRecord]:
    """Evaluate every rule triggered by *record* and persist the alerts it raises."""

    raised: list[AuditRecord] = []
    for rule in rules:
        if (
            record.event_type not in rule.trigger_types
            or record.outcome not in rule.outcomes
        ):
            continue
        scope_value = _scope_value(rule.scope, record)
        if scope_value is None:
            continue
        try:
            count = _count_matching(db_path, rule, scope_value, record)
        except (sqlite3.Error, OSError):
            logger.exception("security_alert_evaluation_failed rule=%s", rule.name)
            continue
        if count < rule.threshold:
            continue

        alert = audit.record_security_event(
            db_path,
            SecurityEvent(
                event_type=rule.alert_type,
                outcome=AuditOutcome.RAISED,
                severity=rule.severity,
                actor_type=ActorType.SYSTEM,
                subject_type=_alert_subject(rule.scope),
                subject_id=scope_value,
                user_id=record.user_id if rule.scope is RuleScope.USER else None,
                request_id=record.request_id,
                source_ref=record.source_ref,
                occurred_at=record.occurred,
                metadata={
                    "rule": rule.name,
                    "scope": rule.scope.value,
                    "count": count,
                    "threshold": rule.threshold,
                    "window_seconds": int(rule.window.total_seconds()),
                    "trigger_seq": record.seq,
                },
            ),
            key=key,
            policy=policy,
        )
        if alert is not None:
            _log_alert(alert)
            raised.append(alert)
    return raised


def record_and_evaluate(
    db_path: str,
    event: SecurityEvent,
    *,
    key: AuditKey | None = None,
    rules: Sequence[ThresholdRule] = DEFAULT_RULES,
    policy: AuditPolicy = audit.DEFAULT_POLICY,
) -> AuditRecord | None:
    """Store *event*, then evaluate the alert rules it can trigger. Never raises."""

    record = audit.record_security_event(db_path, event, key=key, policy=policy)
    if record is not None:
        evaluate_rules(db_path, record, key=key, rules=rules, policy=policy)
    return record


def evaluate_runtime_posture(
    db_path: str,
    *,
    environment: str,
    authentication_enabled: bool,
    anonymous_enabled: bool,
    detected_by: str,
    request_id: str | None = None,
    source_ref: str | None = None,
    key: AuditKey | None = None,
    policy: AuditPolicy = audit.DEFAULT_POLICY,
) -> AuditRecord | None:
    """Raise a critical alert when a production process lacks complete authentication.

    Production startup already fails closed on this state, so the rule is a
    tripwire for a bypassed or regressed guard. ``detected_by`` names the check
    (``startup`` or ``anonymous_request``) so responders know which layer saw it.
    """

    if environment.strip().lower() not in PRODUCTION_ENVIRONMENTS:
        return None
    if authentication_enabled and not anonymous_enabled:
        return None

    alert = audit.record_security_event(
        db_path,
        SecurityEvent(
            event_type=AuditEventType.ALERT_PRODUCTION_AUTH_DISABLED,
            outcome=AuditOutcome.RAISED,
            severity=AuditSeverity.CRITICAL,
            actor_type=ActorType.SYSTEM,
            subject_type=AuditSubjectType.SYSTEM,
            subject_id="web",
            request_id=request_id,
            source_ref=source_ref,
            metadata={
                "rule": "production_auth_disabled",
                "environment": "production",
                "authentication_enabled": authentication_enabled,
                "anonymous_enabled": anonymous_enabled,
                "detected_by": detected_by,
            },
        ),
        key=key,
        policy=policy,
    )
    if alert is not None:
        _log_alert(alert)
    return alert
