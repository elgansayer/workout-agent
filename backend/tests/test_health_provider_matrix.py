from health_provider_matrix import PROVIDER_CAPABILITIES


def test_provider_matrix_keeps_vendor_specific_features_explicit():
    assert "readiness" in PROVIDER_CAPABILITIES["oura"]
    assert "stress" in PROVIDER_CAPABILITIES["garmin"]
    assert "readiness" not in PROVIDER_CAPABILITIES["withings"]
