from connectors.builtin import build_builtin_registry


def test_builtin_health_providers_have_unique_registry_entries():
    registry = build_builtin_registry()
    assert registry.providers() == (
        "fitbit",
        "garmin",
        "garmin_training",
        "oura",
        "polar",
        "withings",
    )
