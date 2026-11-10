"""Purpose-specific health connector consent representation and ledger hooks."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from consent_ledger import (
    CURRENT_CONSENT_POLICY_VERSION,
    ConsentPurpose,
    ConsentRecord,
    ConsentRetention,
    grant_consent,
    has_active_consent,
    revoke_consent,
)
from health_delete import HEALTH_USER_TABLES
from health_retention import DisconnectMode


@dataclass(frozen=True, slots=True)
class HealthConsent:
    user_id: int
    provider: str
    capabilities: frozenset[str]
    policy_version: str
    granted_at: datetime
    revoked_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def __post_init__(self) -> None:
        if self.user_id <= 0 or not self.provider.strip() or not self.policy_version.strip():
            raise ValueError("health consent requires tenant, provider and policy version")


@dataclass(frozen=True, slots=True)
class HealthConsentRevocation:
    """Result of withdrawing health consent."""

    record: ConsentRecord
    purged_rows: int = 0


def grant_health_consent(
    db_path: str,
    *,
    user_id: str | int,
    provider: str,
    scopes: frozenset[str] | set[str] | tuple[str, ...],
    actor_id: str,
    policy_version: str = CURRENT_CONSENT_POLICY_VERSION,
    occurred_at: datetime | None = None,
) -> ConsentRecord:
    """Record that a tenant granted a connector purpose and scopes."""

    return grant_consent(
        db_path,
        user_id=user_id,
        purpose=ConsentPurpose.HEALTH_CONNECTOR,
        policy_version=policy_version,
        actor_id=actor_id,
        scopes=scopes,
        provider=provider,
        occurred_at=occurred_at,
    )


def has_active_health_consent(
    db_path: str,
    *,
    user_id: str | int,
    provider: str,
    required_scopes: frozenset[str] | set[str] | tuple[str, ...] = (),
    policy_version: str = CURRENT_CONSENT_POLICY_VERSION,
) -> bool:
    """Return whether connector processing is currently permitted."""

    return has_active_consent(
        db_path,
        user_id=user_id,
        purpose=ConsentPurpose.HEALTH_CONNECTOR,
        provider=provider,
        required_scopes=required_scopes,
        policy_version=policy_version,
    )


def revoke_health_consent(
    db_path: str,
    *,
    user_id: str | int,
    provider: str,
    actor_id: str,
    scopes: frozenset[str] | set[str] | tuple[str, ...] = (),
    policy_version: str = CURRENT_CONSENT_POLICY_VERSION,
    retention: DisconnectMode = DisconnectMode.REVOKE_ONLY,
    occurred_at: datetime | None = None,
) -> HealthConsentRevocation:
    """Withdraw health consent and apply the documented retention decision.

    ``REVOKE_ONLY`` keeps historical health records until the account or data
    deletion workflow removes them. ``REVOKE_AND_PURGE`` additionally deletes
    the tenant's canonical health rows in the same operation.
    """

    record = revoke_consent(
        db_path,
        user_id=user_id,
        purpose=ConsentPurpose.HEALTH_CONNECTOR,
        policy_version=policy_version,
        actor_id=actor_id,
        scopes=scopes,
        provider=provider,
        retention=ConsentRetention(retention.value),
        occurred_at=occurred_at,
    )
    purged = 0
    if retention is DisconnectMode.REVOKE_AND_PURGE:
        purged = purge_health_consent_data(db_path, user_id=user_id)
    return HealthConsentRevocation(record=record, purged_rows=purged)


def purge_health_consent_data(db_path: str, *, user_id: str | int) -> int:
    """Delete canonical health rows owned by one tenant.

    Only tables that actually exist are touched so this remains safe during a
    rolling deployment where the health schema has not been created yet.
    """

    tenant = str(user_id)
    path = Path(db_path)
    if not path.is_file():
        return 0

    deleted = 0
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        existing = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        for table in HEALTH_USER_TABLES:
            if table not in existing:
                continue
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE user_id = ?",
                (tenant,),
            )
            deleted += max(cursor.rowcount, 0)
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()
    return deleted
