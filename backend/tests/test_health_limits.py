from health_limits import RetryPolicy


def test_retry_backoff_is_bounded():
    policy = RetryPolicy(max_attempts=5, base_delay_seconds=2, max_delay_seconds=10)
    assert [policy.delay(i) for i in range(1, 6)] == [2, 4, 8, 10, 10]
