"""Consent decisions plus the feature-specific withdrawal workflow.

The ledger itself only records the decision. This module applies the documented
consequence of withdrawing each purpose:

* health data: stop future imports and, when the user asks for it, run the
  existing provider-scoped purge (imported metrics, sync runs, connections and
  derived insights) and revoke the stored connector credentials;
* notifications: remove every push subscription so no further push is
  delivered;
* AI processing and analytics: no stored data is removed; future work is denied
  by the consent check in the feature boundary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from consent_ledger import (
    ActorType,
    ConsentPurpose,
    ConsentRecord,
    record_decision,
)
from database import _connect
from health_purge import PurgeResult, purge_imported_data
from secret_vault import (
    CredentialNotFoundError,
    SecretVault,
    UnknownProviderError,
    VaultError,
)

logger = logging.getLogger(__name__)

HEALTH_RETENTION = frozenset({"retain", "purge"})
_HEALTH_PROVIDERS = (
    "google_health",
    "health_connect_file",
    "garmin",
    "health_connect",
    "fitbit",
    "oura",
    "polar",
    "withings",
)


class ConsentWorkflowError(ValueError):
    """The withdrawal request cannot be applied safely."""


@dataclass(slots=True)
class WithdrawalOutcome:
    consent: ConsentRecord
    retention: str
    providers: tuple[str, ...] = ()
    purged: list[PurgeResult] = field(default_factory=list)
    revoked_credentials: tuple[str, ...] = ()
    push_subscriptions_removed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "consent": self.consent.as_dict(),
            "retention": self.retention,
            "providers": list(self.providers),
            "purged": [item.as_dict() for item in self.purged],
            "revoked_credentials": list(self.revoked_credentials),
            "push_subscriptions_removed": self.push_subscriptions_removed,
        }


def _health_providers(connection: Any, user_id: str) -> tuple[str, ...]:
    providers: set[str] = set()
    for table in ("health_connections", "health_metrics"):
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if row is None:
            continue
        for (provider,) in connection.execute(
            f"SELECT DISTINCT provider FROM {table} WHERE user_id = ?",
            (user_id,),
        ).fetchall():
            if provider:
                providers.add(str(provider))
    return tuple(sorted(providers))


def _revoke_credentials(db_path: str, user_id: str, providers: tuple[str, ...]) -> tuple[str, ...]:
    revoked: list[str] = []
    if not providers:
        return ()
    try:
        vault = SecretVault(db_path)
    except VaultError:
        logger.warning("Could not open the credential vault during consent withdrawal.")
        return ()
    for provider in providers:
        try:
            vault.revoke(user_id, provider, actor="owner", source="web")
        except (CredentialNotFoundError, UnknownProviderError):
            continue
        except (VaultError, ValueError):
            logger.warning(
                "Could not revoke the %s credential during consent withdrawal.", provider
            )
            continue
        revoked.append(provider)
    return tuple(revoked)


def withdraw(
    db_path: str,
    user_id: str,
    purpose: ConsentPurpose | str,
    *,
    retention: str = "retain",
    actor_id: str | None = None,
    actor_type: ActorType | str = ActorType.USER,
    source: str = "web",
) -> WithdrawalOutcome:
    """Record the withdrawal and apply the documented consequence."""

    resolved = ConsentPurpose(purpose)
    mode = (retention or "retain").strip().lower()
    if resolved is ConsentPurpose.HEALTH_DATA and mode not in HEALTH_RETENTION:
        raise ConsentWorkflowError("health consent withdrawal retention must be retain or purge")

    providers: tuple[str, ...] = ()
    purged: list[PurgeResult] = []
    removed = 0

    with _connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        record = record_decision(
            connection,
            user_id=user_id,
            purpose=resolved,
            decision="withdrawn",
            actor_type=actor_type,
            actor_id=actor_id,
            source=source,
        )
        if resolved is ConsentPurpose.HEALTH_DATA:
            providers = tuple(
                sorted(set(_health_providers(connection, user_id)) | set(_HEALTH_PROVIDERS))
            )
            if mode == "purge":
                for provider in providers:
                    purged.append(
                        purge_imported_data(
                            connection,
                            user_id=user_id,
                            provider=provider,
                        )
                    )
        elif resolved is ConsentPurpose.NOTIFICATIONS:
            cursor = connection.execute(
                "DELETE FROM push_subscriptions WHERE user_id = ?", (user_id,)
            )
            removed = max(cursor.rowcount, 0)

    revoked = _revoke_credentials(db_path, user_id, providers) if resolved is ConsentPurpose.HEALTH_DATA else ()

    return WithdrawalOutcome(
        consent=record,
        retention=mode,
        providers=providers,
        purged=purged,
        revoked_credentials=revoked,
        push_subscriptions_removed=removed,
    )


__all__ = [
    "HEALTH_RETENTION",
    "ConsentWorkflowError",
    "WithdrawalOutcome",
    "withdraw",
]
