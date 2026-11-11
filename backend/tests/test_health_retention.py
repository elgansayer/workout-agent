import pytest
from health_retention import (
    RETENTION_EXPLANATIONS,
    DisconnectMode,
    DisconnectRequest,
    parse_mode,
    retention_options,
    retention_options_payload,
)


def test_disconnect_is_tenant_bound():
    request = DisconnectRequest(1, "c1", DisconnectMode.REVOKE_ONLY)
    request.validate(authenticated_user_id=1)
    with pytest.raises(ValueError, match="another user"):
        request.validate(authenticated_user_id=2)


def test_purge_requires_explicit_confirmation():
    with pytest.raises(ValueError, match="confirmation"):
        DisconnectRequest(1, "c1", DisconnectMode.REVOKE_AND_PURGE).validate(authenticated_user_id=1)
    DisconnectRequest(1, "c1", DisconnectMode.REVOKE_AND_PURGE, confirmed=True).validate(authenticated_user_id=1)


def test_every_connector_offers_retain_purge_and_revoke_only():
    options = retention_options()
    assert [option.mode for option in options] == [
        DisconnectMode.REVOKE_ONLY,
        DisconnectMode.RETAIN,
        DisconnectMode.PURGE,
    ]
    assert all(option.label and option.explanation for option in options)
    payload = retention_options_payload()
    assert {item["mode"] for item in payload} == {
        "revoke_only",
        "retain",
        "purge",
    }
    assert RETENTION_EXPLANATIONS["purge"]


def test_only_purge_deletes_imported_and_derived_data():
    options = {option.mode: option for option in retention_options()}
    assert not options[DisconnectMode.REVOKE_ONLY].deletes_imported_data
    assert not options[DisconnectMode.RETAIN].deletes_imported_data
    assert options[DisconnectMode.PURGE].deletes_imported_data
    assert options[DisconnectMode.PURGE].deletes_derived_data
    assert DisconnectMode.PURGE.purges_imported_data
    assert DisconnectMode.REVOKE_AND_PURGE.purges_imported_data
    assert not DisconnectMode.RETAIN.purges_imported_data


def test_legacy_purge_alias_collapses_to_canonical_purge():
    request = DisconnectRequest(1, "c1", DisconnectMode.REVOKE_AND_PURGE, confirmed=True)
    assert request.effective_mode is DisconnectMode.PURGE


def test_parse_mode_rejects_unknown_values():
    assert parse_mode("PURGE") is DisconnectMode.PURGE
    assert parse_mode(DisconnectMode.RETAIN) is DisconnectMode.RETAIN
    with pytest.raises(ValueError, match="retention mode"):
        parse_mode("delete-everything")


def test_purge_requires_non_empty_connection_id():
    with pytest.raises(ValueError, match="connection_id"):
        DisconnectRequest(1, "  ", DisconnectMode.REVOKE_ONLY).validate(authenticated_user_id=1)
