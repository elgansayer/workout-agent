import pytest

from health_contract_fixture import ContractFixture


def test_contract_fixture_pins_provider_version():
    fixture = ContractFixture.create("oura", {"id": "synthetic"})
    assert fixture.contract == "api-v2"


def test_contract_fixture_rejects_embedded_secret():
    with pytest.raises(ValueError):
        ContractFixture.create("oura", {"access_token": "secret"})
