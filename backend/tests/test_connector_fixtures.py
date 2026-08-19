import pytest

from connectors.fixtures import assert_safe_fixture


def test_safe_synthetic_fixture_is_allowed():
    assert_safe_fixture({"id": "synthetic-1", "sleep": {"minutes": 480}})


def test_secret_bearing_fixture_is_rejected_recursively():
    with pytest.raises(ValueError, match="secret-bearing"):
        assert_safe_fixture({"oauth": {"refresh_token": "not-even-a-test-token"}})
