from health_feature_flags import provider_enabled


def test_approval_gated_provider_is_not_enabled_without_configuration():
    assert not provider_enabled("garmin", configured=False)
    assert provider_enabled("garmin", configured=True)


def test_health_connect_companion_path_can_be_present_without_server_oauth():
    assert provider_enabled("health_connect", configured=False)
