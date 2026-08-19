from health_data_quality import DataQuality, QualityFlag


def test_stale_or_outlier_data_is_not_used_for_adaptation():
    assert DataQuality().usable_for_adaptation
    assert not DataQuality(frozenset({QualityFlag.STALE})).usable_for_adaptation
    assert not DataQuality(frozenset({QualityFlag.OUTLIER})).usable_for_adaptation
