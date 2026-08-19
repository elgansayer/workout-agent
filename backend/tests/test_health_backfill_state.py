from datetime import date

from health_backfill import BackfillWindow
from health_backfill_state import BackfillState


def test_backfill_state_preserves_connection_cursor():
    state = BackfillState(1, "oura-1", BackfillWindow(date(2026, 8, 1), date(2026, 8, 19)), cursor="page-2")
    state.validate()
    assert state.cursor == "page-2"
