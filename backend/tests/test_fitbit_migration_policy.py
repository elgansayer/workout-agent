import pytest

from connectors.base import ConnectorContext, ConnectorError, ConnectorState
from connectors.fitbit import (
    FITBIT_WEB_API_SHUTDOWN_MONTH,
    GOOGLE_HEALTH_PROVIDER,
    FitbitConnector,
)


@pytest.fixture
def connector() -> FitbitConnector:
    return FitbitConnector()


@pytest.fixture
def context() -> ConnectorContext:
    return ConnectorContext(user_id="user-test")


def test_legacy_fitbit_connector_cannot_authorize_or_sync_new_connections(
    connector: FitbitConnector,
) -> None:
    assert connector.capabilities.authorize is False
    assert connector.capabilities.sync is False
    assert connector.capabilities.refresh is False
    assert connector.capabilities.backfill is False
    assert connector.capabilities.webhooks is False
    assert connector.capabilities.write is False


def test_legacy_fitbit_status_requires_google_health_migration(
    connector: FitbitConnector,
    context: ConnectorContext,
) -> None:
    status = connector.status(context)

    assert status.state is ConnectorState.ATTENTION
    assert status.metadata == {
        "migration_target": GOOGLE_HEALTH_PROVIDER,
        "reconsent_required": True,
        "legacy_shutdown_month": FITBIT_WEB_API_SHUTDOWN_MONTH,
        "new_connections_allowed": False,
        "identity_mapping": "users.getIdentity",
    }
    assert "September 2026" in (status.message or "")


@pytest.mark.parametrize("operation", ["authorize", "sync", "normalize_record"])
def test_legacy_fitbit_data_operations_fail_with_migration_error(
    connector: FitbitConnector,
    context: ConnectorContext,
    operation: str,
) -> None:
    with pytest.raises(ConnectorError) as exc_info:
        if operation == "authorize":
            connector.authorize(context)
        elif operation == "sync":
            connector.sync(context)
        else:
            list(connector.normalize_record(context, {}))

    assert exc_info.value.code == "legacy_provider_migration_required"
    assert "Google Health API" in str(exc_info.value)
