from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from consent_ledger import (
    CURRENT_CONSENT_POLICY_VERSION,
    ConsentActorType,
    ConsentDecisionRequest,
    ConsentPurpose,
    ConsentRequiredError,
    ConsentRetention,
    append_decision,
    current_consent,
    current_consents,
    grant_consent,
    has_active_consent,
    require_active_consent,
    revoke_consent,
    verify_chain,
)
from database import get_or_create_user, init_db
from health_consent import (
    grant_health_consent,
    has_active_health_consent,
    revoke_health_consent,
)
from health_retention import DisconnectMode


def _db(tmp_path) -> str:
    db_path = str(tmp_path / "consent.db")
    init_db(db_path)
    return db_path


def _user(db_path: str, email: str = "synthetic@example.com") -> str:
    return get_or_create_user(email, "Synthetic User", db_path)["id"]


def test_grant_records_policy_version_purpose_scopes_actor_and_time(tmp_path):
    db_path = _db(tmp_path)
    user_id = _user(db_path)
    now = datetime(2026, 8, 17, 9, 30, tzinfo=timezone.utc)

    record = grant_consent(
        db_path,
        user_id=user_id,
        purpose=ConsentPurpose.HEALTH_CONNECTOR,
        policy_version="1.0.0",
        actor_id=user_id,
        scopes=["sleep", "heart_rate", "sleep"],
        provider="Oura",
        occurred_at=now,
    )

    assert record.user_id == user_id
    assert record.purpose == "health_connector"
    assert record.provider == "oura"
    assert record.policy_version == "1.0.0"
    assert record.scopes == frozenset({"sleep", "heart_rate"})
    assert record.actor_id == user_id
    assert record.actor_type == "user"
    assert record.occurred_at.startswith("2026-08-17T09:30:00")
    assert record.active
    assert record.revocation_state == "active"
    assert record.revoked_at is None
    assert record.retention == ConsentRetention.REVOKE_ONLY.value


def test_init_db_creates_tenant_scoped_ledger(tmp_path):
    db_path = _db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(consent_ledger_events)")
        }
        indexes = {
            row[1]
            for row in conn.execute(
                "SELECT * FROM sqlite_master WHERE type = 'index'"
            )
        }

    assert {
        "user_id",
        "purpose",
        "provider",
        "policy_version",
        "scopes_json",
        "actor_id",
        "decision",
        "retention",
        "occurred_at",
    }.issubset(columns)
    assert "idx_consent_ledger_events_user" in indexes


def test_current_consent_is_tenant_isolated(tmp_path):
    db_path = _db(tmp_path)
    alice = _user(db_path, "alice@example.com")
    bob = _user(db_path, "bob@example.com")

    grant_health_consent(
        db_path,
        user_id=alice,
        provider="oura",
        scopes={"sleep"},
        actor_id=alice,
    )

    assert has_active_health_consent(
        db_path, user_id=alice, provider="oura", required_scopes={"sleep"}
    )
    assert not has_active_health_consent(db_path, user_id=bob, provider="oura")
    assert current_consent(
        db_path, user_id=bob, purpose=ConsentPurpose.HEALTH_CONNECTOR, provider="oura"
    ) is None


def test_revocation_appends_history_and_fails_closed(tmp_path):
    db_path = _db(tmp_path)
    user_id = _user(db_path)
    granted_at = datetime(2026, 8, 17, tzinfo=timezone.utc)
    revoked_at = granted_at + timedelta(days=1)

    grant_consent(
        db_path,
        user_id=user_id,
        purpose=ConsentPurpose.NOTIFICATIONS,
        policy_version=CURRENT_CONSENT_POLICY_VERSION,
        actor_id=user_id,
        scopes=["web_push"],
        provider="web_push",
        occurred_at=granted_at,
    )
    revoked = revoke_consent(
        db_path,
        user_id=user_id,
        purpose=ConsentPurpose.NOTIFICATIONS,
        actor_id=user_id,
        scopes=["web_push"],
        provider="web_push",
        occurred_at=revoked_at,
    )

    assert not revoked.active
    assert revoked.revocation_state == "revoked"
    assert revoked.revoked_at == revoked.occurred_at
    assert not has_active_consent(
        db_path,
        user_id=user_id,
        purpose=ConsentPurpose.NOTIFICATIONS,
        provider="web_push",
        required_scopes=["web_push"],
    )
    with pytest.raises(ConsentRequiredError, match="withdrawn"):
        require_active_consent(
            db_path,
            user_id=user_id,
            purpose=ConsentPurpose.NOTIFICATIONS,
            provider="web_push",
        )

    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM consent_ledger_events WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
    assert count == 2


def test_policy_version_and_scope_are_checked(tmp_path):
    db_path = _db(tmp_path)
    user_id = _user(db_path)
    grant_consent(
        db_path,
        user_id=user_id,
        purpose=ConsentPurpose.AI_PROCESSING,
        policy_version="1.0.0",
        actor_id=user_id,
        scopes=["claude"],
        provider="claude",
    )

    assert has_active_consent(
        db_path,
        user_id=user_id,
        purpose=ConsentPurpose.AI_PROCESSING,
        provider="claude",
        required_scopes=["claude"],
        policy_version="1.0.0",
    )
    assert not has_active_consent(
        db_path,
        user_id=user_id,
        purpose=ConsentPurpose.AI_PROCESSING,
        provider="claude",
        required_scopes=["claude"],
        policy_version="2.0.0",
    )
    assert not has_active_consent(
        db_path,
        user_id=user_id,
        purpose=ConsentPurpose.AI_PROCESSING,
        provider="claude",
        required_scopes=["openai"],
    )


def test_ledger_rejects_update_and_delete(tmp_path):
    db_path = _db(tmp_path)
    user_id = _user(db_path)
    grant_consent(
        db_path,
        user_id=user_id,
        purpose=ConsentPurpose.ANALYTICS,
        policy_version="1.0.0",
        actor_id=user_id,
    )

    with sqlite3.connect(db_path) as conn, pytest.raises(
        sqlite3.DatabaseError, match="append-only"
    ):
        conn.execute(
            "UPDATE consent_ledger_events SET decision = 'revoked' WHERE user_id = ?",
            (user_id,),
        )
    with sqlite3.connect(db_path) as conn, pytest.raises(
        sqlite3.DatabaseError, match="append-only"
    ):
        conn.execute(
            "DELETE FROM consent_ledger_events WHERE user_id = ?",
            (user_id,),
        )


def test_hash_chain_detects_rewritten_history(tmp_path):
    db_path = _db(tmp_path)
    user_id = _user(db_path)
    grant_consent(
        db_path,
        user_id=user_id,
        purpose=ConsentPurpose.ANALYTICS,
        policy_version="1.0.0",
        actor_id=user_id,
    )
    revoke_consent(
        db_path,
        user_id=user_id,
        purpose=ConsentPurpose.ANALYTICS,
        actor_id=user_id,
    )

    verification = verify_chain(db_path)
    assert verification.ok
    assert verification.checked == 2

    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TRIGGER trg_consent_ledger_events_no_update")
        conn.execute(
            "UPDATE consent_ledger_events SET policy_version = '9.9.9' WHERE seq = 1"
        )
        conn.commit()

    assert not verify_chain(db_path).ok


def test_current_consents_returns_latest_decision_per_purpose(tmp_path):
    db_path = _db(tmp_path)
    user_id = _user(db_path)
    grant_consent(
        db_path,
        user_id=user_id,
        purpose=ConsentPurpose.HEALTH_CONNECTOR,
        policy_version="1.0.0",
        actor_id=user_id,
        scopes=["sleep"],
        provider="oura",
    )
    grant_consent(
        db_path,
        user_id=user_id,
        purpose=ConsentPurpose.NOTIFICATIONS,
        policy_version="1.0.0",
        actor_id=user_id,
        scopes=["web_push"],
        provider="web_push",
    )

    latest = current_consents(db_path, user_id=user_id)

    assert [(item.purpose, item.provider) for item in latest] == [
        ("health_connector", "oura"),
        ("notifications", "web_push"),
    ]
    assert all(item.active for item in latest)


def test_withdrawal_purges_only_the_tenant_when_requested(tmp_path):
    db_path = _db(tmp_path)
    alice = _user(db_path, "alice@example.com")
    bob = _user(db_path, "bob@example.com")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE health_metrics (id INTEGER PRIMARY KEY, user_id TEXT)"
        )
        conn.executemany(
            "INSERT INTO health_metrics (user_id) VALUES (?)",
            [(alice,), (alice,), (bob,)],
        )
        conn.commit()

    grant_health_consent(
        db_path,
        user_id=alice,
        provider="oura",
        scopes={"sleep"},
        actor_id=alice,
    )
    outcome = revoke_health_consent(
        db_path,
        user_id=alice,
        provider="oura",
        actor_id=alice,
        retention=DisconnectMode.REVOKE_AND_PURGE,
    )

    assert outcome.purged_rows == 2
    assert outcome.record.retention == ConsentRetention.REVOKE_AND_PURGE.value
    assert not outcome.record.active
    with sqlite3.connect(db_path) as conn:
        remaining = conn.execute(
            "SELECT user_id FROM health_metrics ORDER BY user_id"
        ).fetchall()
    assert remaining == [(bob,)]


def test_invalid_decisions_are_rejected(tmp_path):
    db_path = _db(tmp_path)
    user_id = _user(db_path)

    with pytest.raises(ValueError, match="provider"):
        grant_consent(
            db_path,
            user_id=user_id,
            purpose=ConsentPurpose.HEALTH_CONNECTOR,
            policy_version="1.0.0",
            actor_id=user_id,
            scopes=["sleep"],
        )
    with pytest.raises(ValueError, match="scope"):
        grant_consent(
            db_path,
            user_id=user_id,
            purpose=ConsentPurpose.HEALTH_CONNECTOR,
            policy_version="1.0.0",
            actor_id=user_id,
            provider="oura",
        )
    with pytest.raises(ValueError, match="actor"):
        grant_consent(
            db_path,
            user_id=user_id,
            purpose=ConsentPurpose.ANALYTICS,
            policy_version="1.0.0",
            actor_id="",
        )
    with pytest.raises(ValueError, match="tenant"):
        grant_consent(
            db_path,
            user_id="",
            purpose=ConsentPurpose.ANALYTICS,
            policy_version="1.0.0",
            actor_id=user_id,
        )
    with pytest.raises(ValueError, match="timezone"):
        append_decision(
            db_path,
            ConsentDecisionRequest(
                user_id=user_id,
                purpose=ConsentPurpose.ANALYTICS,
                policy_version="1.0.0",
                actor_id=user_id,
                occurred_at=datetime.fromisoformat("2026-08-17"),
            ),
        )


def test_system_decisions_do_not_require_a_user_actor(tmp_path):
    db_path = _db(tmp_path)
    user_id = _user(db_path)
    record = append_decision(
        db_path,
        ConsentDecisionRequest(
            user_id=user_id,
            purpose=ConsentPurpose.ANALYTICS,
            policy_version="1.0.0",
            actor_id="",
            actor_type=ConsentActorType.SYSTEM,
        ),
    )

    assert record.actor_type == "system"
    assert record.actor_id == "system"
