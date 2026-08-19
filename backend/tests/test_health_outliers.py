from health_outliers import is_relative_outlier


def test_outlier_detection_requires_history_and_large_deviation():
    assert not is_relative_outlier(100, [60] * 3)
    assert not is_relative_outlier(65, [60] * 14)
    assert is_relative_outlier(100, [60] * 14)
