def test_health_platform_modules_import():
    import adaptive_training  # noqa: F401
    import health_dedup  # noqa: F401
    import health_ingestion  # noqa: F401
    import health_models  # noqa: F401
    import health_summary  # noqa: F401
    import recovery  # noqa: F401
    import connectors.base  # noqa: F401
    import connectors.builtin  # noqa: F401
    import connectors.conformance  # noqa: F401
    import connectors.fitbit  # noqa: F401
    import connectors.garmin  # noqa: F401
    import connectors.health_connect  # noqa: F401
    import connectors.oura  # noqa: F401
    import connectors.polar  # noqa: F401
    import connectors.withings  # noqa: F401
