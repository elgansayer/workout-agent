from health_provider_contracts import contract_version


def test_current_provider_contracts_are_explicit():
    assert contract_version("oura") == "api-v2"
    assert contract_version("polar") == "accesslink-dynamic-v4"
    assert "validation" in contract_version("fitbit")
    assert contract_version("health_connect") == "android-sdk-companion"
