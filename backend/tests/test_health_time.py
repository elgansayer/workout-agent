from datetime import datetime, timedelta, timezone

import pytest

from health_time import to_utc


def test_health_timestamp_converts_to_utc():
    local = datetime(2026, 8, 19, 12, tzinfo=timezone(timedelta(hours=2)))
    assert to_utc(local).hour == 10


def test_naive_health_timestamp_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        to_utc(datetime(2026, 8, 19, 12))
