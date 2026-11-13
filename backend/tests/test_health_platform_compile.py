def test_health_platform_modules_import():
    import adaptive_training  # noqa: F401
    import connectors.base
    import connectors.builtin
    import connectors.conformance
    import connectors.fitbit
    import connectors.garmin
    import connectors.health_connect
    import connectors.oura
    import connectors.polar
    import connectors.withings  # noqa: F401
    import health_dedup  # noqa: F401
    import health_ingestion  # noqa: F401
    import health_models  # noqa: F401
    import health_summary  # noqa: F401
    import recovery  # noqa: F401
