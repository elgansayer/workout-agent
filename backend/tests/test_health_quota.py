from health_quota import QuotaState


def test_quota_exhaustion_is_explicit():
    assert QuotaState("polar", 0, None).exhausted
    assert not QuotaState("polar", 10, None).exhausted
    assert not QuotaState("polar", None, None).exhausted
