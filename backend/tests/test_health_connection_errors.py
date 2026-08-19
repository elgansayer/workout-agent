from health_connection_errors import ConnectionAttention


def test_connector_attention_reasons_are_stable_api_values():
    assert ConnectionAttention.REAUTHENTICATE.value == "reauthenticate"
    assert ConnectionAttention.APPROVAL_REQUIRED.value == "approval_required"
    assert ConnectionAttention.COMPANION_OFFLINE.value == "companion_offline"
