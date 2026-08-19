from datetime import datetime, timezone

from health_provider_scores import ProviderScore


def test_provider_score_preserves_vendor_identity():
    score = ProviderScore(1, "oura", "readiness", 82, datetime(2026, 8, 19, tzinfo=timezone.utc), "oura-1")
    assert score.provider == "oura"
    assert score.score_name == "readiness"
