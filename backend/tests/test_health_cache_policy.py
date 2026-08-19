from health_cache_policy import PERSONALIZED_HEALTH_HEADERS


def test_personalized_health_responses_are_never_cacheable():
    assert "no-store" in PERSONALIZED_HEALTH_HEADERS["Cache-Control"]
    assert "Authorization" in PERSONALIZED_HEALTH_HEADERS["Vary"]
