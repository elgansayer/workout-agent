from datetime import date, timedelta

import pytest

from health_backfill import BackfillWindow


def test_backfill_accepts_bounded_window():
    BackfillWindow(date(2026, 8, 1), date(2026, 8, 19)).validate()


def test_backfill_rejects_unbounded_history():
    start = date(2025, 1, 1)
    with pytest.raises(ValueError, match="maximum"):
        BackfillWindow(start, start + timedelta(days=366)).validate()
