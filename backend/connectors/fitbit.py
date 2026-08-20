"""Legacy Fitbit Web API migration sentinel.

Google will turn down the legacy Fitbit Web API in September 2026. New health
connections must use the Google Health API instead. This connector remains in
the registry only so existing Fitbit-backed accounts can be identified and
migrated deliberately; it must never become a new production authorization or
sync path.

See ``docs/FITBIT_GOOGLE_HEALTH_MIGRATION.md`` for the accepted migration
strategy and rollback rules.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .base import (
    ConnectorCapabilities,
    ConnectorContext,
    ConnectorError,
    ConnectorState,
    ConnectorStatus,
    SyncResult,
)
from .provider import HealthProviderConnector, NormalizedRecord

GOOGLE_HEALTH_PROVIDER = "google_health"
FITBIT_WEB_API_SHUTDOWN_MONTH = "2026-09"
_MIGRATION_ERROR_CODE = "legacy_provider_migration_required"


class FitbitConnector(HealthProviderConnector):
    """Represent legacy Fitbit connections without permitting new activation."""

    provider = "fitbit"
    capabilities = ConnectorCapabilities(
        authorize=False,
        test=True,
        sync=False,
        refresh=False,
        disconnect=True,
        purge=True,
        backfill=False,
        webhooks=False,
        write=False,
        metrics=frozenset(
            {"sleep", "activity", "heart_rate", "workouts", "body"}
        ),
    )

    @staticmethod
    def _migration_error() -> ConnectorError:
        return ConnectorError(
            "The legacy Fitbit Web API is migration-only. Reconnect with the "
            "Google Health API to continue syncing health data.",
            code=_MIGRATION_ERROR_CODE,
        )

    def status(self, context: ConnectorContext) -> ConnectorStatus:
        return ConnectorStatus(
            self.provider,
            ConnectorState.ATTENTION,
            message=(
                "Legacy Fitbit Web API access is migration-only and is scheduled "
                "to shut down in September 2026. Reconnect with Google Health."
            ),
            metadata={
                "migration_target": GOOGLE_HEALTH_PROVIDER,
                "reconsent_required": True,
                "legacy_shutdown_month": FITBIT_WEB_API_SHUTDOWN_MONTH,
                "new_connections_allowed": False,
                "identity_mapping": "users.getIdentity",
            },
        )

    def authorize(
        self,
        context: ConnectorContext,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        raise self._migration_error()

    def test(self, context: ConnectorContext) -> ConnectorStatus:
        return self.status(context)

    def sync(
        self,
        context: ConnectorContext,
        *,
        cursor: str | None = None,
    ) -> SyncResult:
        raise self._migration_error()

    def disconnect(self, context: ConnectorContext) -> None:
        return None

    def purge(self, context: ConnectorContext) -> None:
        return None

    def normalize_record(
        self,
        context: ConnectorContext,
        payload: Mapping[str, Any],
    ) -> Iterable[NormalizedRecord]:
        raise self._migration_error()
