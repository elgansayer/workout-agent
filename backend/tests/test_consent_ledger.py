"""Append-only consent decisions, tenant scoping and withdrawal workflows."""

from __future__ import annotations

import sqlite3

import consent_ledger
import pytest
from consent_ledger import (
    ConsentPurpose,
    current_decision,
    grant_consent_at,
    has_consent,
    has_consent_at,
    snapshot,
    withdraw_consent_at,
)
from consent_service import withdraw
from database import (
    get_or_create_user,
    get_push_subscriptions,
    init_db,
    save_push_subscription,
)


@pytest.fixture()
def db(tmp_path) -> str:
    path = str(tmp_path / "consent.db")
    init_db(path)
    return path


def _user(db: str, name: str) -> str:
    return get_or_create_user(f"{name}@example.com", name.title(), db)["id"]


def test_a_grant_records_policy_purpose_scopes_time_actor_and_state(db: str) -> None:
    user_id = _user(db, "alice")
    record = grant_consent_at(
        db,
        user_id,
        ConsentPurpose.HEALTH_DATA,
        scopes=("sleep", "body"),
        actor_id=user_id,
    )

    assert record.purpose is ConsentPurpose.HEALTH_DATA
    assert record.policy_version == consent_ledger.POLICY_VERSION
    assert record.scopes == ("sleep", "body")
    assert record.actor_type.value == "user"
    assert record.actor_id == user_id
    assert record.decision.value == "granted"
    assert record.active
    assert record.revoked_at is None
    assert record.decided_at

    with sqlite3.connect(db) as conn:
        current = current_decision(conn, user_id, ConsentPurpose.HEALTH_DATA)
        assert current == record
        assert has_consent(
            conn, user_id, ConsentPurpose.HEALTH_DATA, required_scopes=("sleep",)
        )
        assert not has_consent(
            conn, user_id, ConsentPurpose.HEALTH_DATA, required_scopes=("spo2",)
        )


def test_withdrawal_is_appended_and_blocks_future_processing(db: str) -> None:
    user_id = _user(db, "alice")
    grant_consent_at(db, user_id, ConsentPurpose.AI_PROCESSING, actor_id=user_id)
    assert has_consent_at(db, user_id, ConsentPurpose.AI_PROCESSING)

    revoked = withdraw_consent_at(
        db, user_id, ConsentPurpose.AI_PROCESSING, actor_id=user_id
    )

    assert revoked.decision.value == "withdrawn"
    assert revoked.revoked_at is not None
    assert not revoked.active
    assert not has_consent_at(db, user_id, ConsentPurpose.AI_PROCESSING)

    with sqlite3.connect(db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM consent_decisions WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        assert count == 2
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE consent_decisions SET decision = 'granted' WHERE user_id = ?",
                (user_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM consent_decisions WHERE user_id = ?", (user_id,)
            )


def test_a_stale_policy_version_requires_reconsent(db: str) -> None:
    user_id = _user(db, "alice")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO consent_decisions "
            "(record_id, user_id, purpose, scope, policy_version, scopes_json, "
            "decision, actor_type, actor_id, decided_at, revoked_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-policy-record",
                user_id,
                ConsentPurpose.NOTIFICATIONS.value,
                "*",
                "1999.1",
                "[]",
                "granted",
                "user",
                user_id,
                "2026-01-01T00:00:00+00:00",
                None,
                "test",
            ),
        )
    with sqlite3.connect(db) as conn:
        assert not has_consent(conn, user_id, ConsentPurpose.NOTIFICATIONS)
        state = snapshot(conn, user_id)
    purpose = next(
        item for item in state["purposes"] if item["purpose"] == "notifications"
    )
    assert purpose["needs_reconsent"] is True


def test_consent_is_tenant_scoped(db: str) -> None:
    alice = _user(db, "alice")
    bob = _user(db, "bob")
    grant_consent_at(db, alice, ConsentPurpose.HEALTH_DATA, actor_id=alice)

    alice_state = consent_ledger.snapshot_at(db, alice)
    bob_state = consent_ledger.snapshot_at(db, bob)
    alice_health = next(
        item for item in alice_state["purposes"] if item["purpose"] == "health_data"
    )
    bob_health = next(
        item for item in bob_state["purposes"] if item["purpose"] == "health_data"
    )
    assert alice_health["granted"] is True
    assert bob_health["granted"] is False
    assert bob_health["current"] is None


def test_an_unknown_or_unsupported_scope_is_rejected(db: str) -> None:
    user_id = _user(db, "alice")
    with pytest.raises(ValueError, match="unsupported consent scopes"):
        grant_consent_at(
            db,
            user_id,
            ConsentPurpose.HEALTH_DATA,
            scopes=("not_a_scope",),
            actor_id=user_id,
        )
    with pytest.raises(ValueError, match="unknown consent purpose"):
        grant_consent_at(db, user_id, "not_a_purpose", actor_id=user_id)


def test_backfill_records_an_existing_explicit_opt_in_once(db: str) -> None:
    user_id = _user(db, "alice")
    save_push_subscription(user_id, "https://push.example/1", "p", "a", db)
    with sqlite3.connect(db) as conn:
        consent_ledger.backfill_enabled_features(conn)
        consent_ledger.backfill_enabled_features(conn)
        record = current_decision(conn, user_id, ConsentPurpose.NOTIFICATIONS)
        assert record is not None and record.active
        assert record.source == "migration:push_subscription"
        count = conn.execute(
            "SELECT COUNT(*) FROM consent_decisions WHERE user_id = ? AND purpose = ?",
            (user_id, ConsentPurpose.NOTIFICATIONS.value),
        ).fetchone()[0]
        assert count == 1


def test_withdrawing_health_consent_purges_imported_data(db: str) -> None:
    user_id = _user(db, "alice")
    grant_consent_at(db, user_id, ConsentPurpose.HEALTH_DATA, actor_id=user_id)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO health_connections "
            "(id, user_id, provider, state, granted_capabilities) VALUES (?, ?, ?, ?, ?)",
            ("conn-1", user_id, "oura", "connected", "[]"),
        )
        conn.execute(
            "INSERT INTO health_metrics "
            "(user_id, metric_type, value, unit, observed_at, provider, connection_id, fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, "sleep", 7.5, "h", "2026-08-01T00:00:00+00:00", "oura", "conn-1", "fp-1"),
        )

    outcome = withdraw(
        db, user_id, ConsentPurpose.HEALTH_DATA, retention="purge", actor_id=user_id
    )

    assert outcome.consent.decision.value == "withdrawn"
    assert outcome.purged
    assert not has_consent_at(db, user_id, ConsentPurpose.HEALTH_DATA)
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM health_metrics WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM health_connections WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            == 0
        )


def test_retaining_health_data_still_stops_processing(db: str) -> None:
    user_id = _user(db, "alice")
    grant_consent_at(db, user_id, ConsentPurpose.HEALTH_DATA, actor_id=user_id)

    outcome = withdraw(
        db, user_id, ConsentPurpose.HEALTH_DATA, retention="retain", actor_id=user_id
    )

    assert outcome.purged == []
    assert not has_consent_at(db, user_id, ConsentPurpose.HEALTH_DATA)


def test_withdrawing_notification_consent_removes_push_subscriptions(db: str) -> None:
    user_id = _user(db, "alice")
    grant_consent_at(db, user_id, ConsentPurpose.NOTIFICATIONS, actor_id=user_id)
    save_push_subscription(user_id, "https://push.example/1", "p", "a", db)
    assert get_push_subscriptions(user_id, db_path=db)

    outcome = withdraw(
        db, user_id, ConsentPurpose.NOTIFICATIONS, actor_id=user_id
    )

    assert outcome.push_subscriptions_removed == 1
    assert get_push_subscriptions(user_id, db_path=db) == []
    assert not has_consent_at(db, user_id, ConsentPurpose.NOTIFICATIONS)


def test_has_consent_fails_closed_for_an_unknown_tenant(db: str) -> None:
    assert not has_consent_at(db, "missing-user", ConsentPurpose.AI_PROCESSING)
