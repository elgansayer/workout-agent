from health_api_contract import HEALTH_API_ROUTES, PUBLIC_HEALTH_API_ROUTES


def test_health_api_has_no_public_personalized_routes():
    assert HEALTH_API_ROUTES
    assert PUBLIC_HEALTH_API_ROUTES == frozenset()
    assert all(path.startswith("/api/health/") for _, path in HEALTH_API_ROUTES.values())
