from datetime import datetime, timedelta, timezone

import pytest

from health_activity_reconcile import SessionWindow, reconcile_sessions


NOW = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)


def session(user, source, ident, start, minutes=60):
    return SessionWindow(user, source, ident, start, start + timedelta(minutes=minutes))


def test_overlapping_garmin_and_hevy_sessions_link_without_merging_ids():
    link = reconcile_sessions(session(1, "garmin", "g1", NOW), session(1, "hevy", "h1", NOW + timedelta(minutes=5)))
    assert link is not None
    assert (link.left_id, link.right_id) == ("g1", "h1")
    assert link.overlap_seconds > 0


def test_reconciliation_rejects_cross_user_records():
    with pytest.raises(ValueError, match="across users"):
        reconcile_sessions(session(1, "garmin", "g1", NOW), session(2, "hevy", "h1", NOW))
