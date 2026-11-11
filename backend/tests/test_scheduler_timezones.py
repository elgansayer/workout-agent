"""The unified scheduler fires each user's jobs in that user's own time zone."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

import scheduler

SCHEDULE = {
    "coaching": [(7, 0)],
    "insight_daily": [(6, 0)],
    "insight_weekly": [(8, 0)],
}


def _utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _due(users: list[dict[str, Any]], now: datetime, done: set[Any] | None = None) -> set[tuple[str, str]]:
    return {(d.user_id, d.job) for d in scheduler.due_jobs(users, now, SCHEDULE, done or set())}


# ---------------------------------------------------------------------------
# Same instant, different local times
# ---------------------------------------------------------------------------


def test_each_user_is_due_at_their_own_local_run_time() -> None:
    users = [
        {"id": "london", "timezone": "Europe/London"},  # UTC+1 in June
        {"id": "utc", "timezone": "UTC"},
        {"id": "new-york", "timezone": "America/New_York"},  # UTC-4 in June
        {"id": "tokyo", "timezone": "Asia/Tokyo"},  # UTC+9
    ]

    assert _due(users, _utc(2026, 6, 15, 6, 0)) == {
        ("london", "coaching"),  # 07:00 BST
        ("utc", "insight_daily"),  # 06:00 UTC
    }
    assert _due(users, _utc(2026, 6, 15, 11, 0)) == {
        ("new-york", "coaching"),  # 07:00 EDT
    }
    assert ("tokyo", "insight_daily") in _due(users, _utc(2026, 6, 14, 21, 0))  # 06:00 JST


def test_the_container_time_zone_is_irrelevant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler, "_TZ", "Pacific/Kiritimati")
    assert _due([{"id": "u", "timezone": "UTC"}], _utc(2026, 6, 15, 7, 0)) == {("u", "coaching")}


@pytest.mark.parametrize("zone", [None, "", "Mars/Base", "../../etc/passwd", 5])
def test_missing_or_invalid_zones_are_scheduled_in_utc(zone: Any) -> None:
    assert _due([{"id": "u", "timezone": zone}], _utc(2026, 6, 15, 7, 0)) == {("u", "coaching")}


def test_rows_without_an_id_are_skipped() -> None:
    assert _due([{"timezone": "UTC"}, {"id": "", "timezone": "UTC"}], _utc(2026, 6, 15, 7, 0)) == set()


def test_changing_a_users_zone_takes_effect_on_the_next_evaluation() -> None:
    now = _utc(2026, 6, 15, 7, 0)
    assert _due([{"id": "u", "timezone": "UTC"}], now) == {("u", "coaching")}
    assert _due([{"id": "u", "timezone": "Asia/Tokyo"}], now) == set()


# ---------------------------------------------------------------------------
# Daylight-saving transitions
# ---------------------------------------------------------------------------

NEW_YORK = [{"id": "ny", "timezone": "America/New_York"}]


@pytest.mark.parametrize(
    ("instant", "is_due"),
    [
        # Spring forward 2026-03-08: 07:00 is EDT (UTC-4), i.e. 11:00Z, not 12:00Z.
        (_utc(2026, 3, 8, 10, 59), False),
        (_utc(2026, 3, 8, 11, 0), True),
        (_utc(2026, 3, 8, 11, 59), True),
        (_utc(2026, 3, 8, 12, 0), False),  # 08:00 EDT: window closed
        # The day before still follows EST (UTC-5): 07:00 is 12:00Z.
        (_utc(2026, 3, 7, 11, 0), False),
        (_utc(2026, 3, 7, 12, 0), True),
        # Fall back 2026-11-01: 07:00 is EST (UTC-5), i.e. 12:00Z, not 11:00Z.
        (_utc(2026, 11, 1, 11, 0), False),
        (_utc(2026, 11, 1, 12, 0), True),
        (_utc(2026, 10, 31, 11, 0), True),  # the day before is still EDT
    ],
)
def test_run_time_tracks_the_dst_offset_in_force_that_day(instant: datetime, is_due: bool) -> None:
    assert (("ny", "coaching") in _due(NEW_YORK, instant)) is is_due


def test_a_slot_skipped_by_spring_forward_still_runs_once() -> None:
    schedule = {"coaching": [(2, 30)]}  # 02:30 does not exist on 2026-03-08
    users = NEW_YORK
    done: set[tuple[str, str, str]] = set()

    before_gap = scheduler.due_jobs(users, _utc(2026, 3, 8, 6, 59), schedule, done)
    after_gap = scheduler.due_jobs(users, _utc(2026, 3, 8, 7, 0), schedule, done)  # 03:00 EDT

    assert before_gap == []
    assert [(d.user_id, d.job) for d in after_gap] == [("ny", "coaching")]


def test_the_repeated_fall_back_hour_runs_a_slot_only_once() -> None:
    schedule = {"coaching": [(1, 30)]}
    done: set[tuple[str, str, str]] = set()

    first = scheduler.due_jobs(NEW_YORK, _utc(2026, 11, 1, 5, 30), schedule, done)  # 01:30 EDT
    for item in first:
        done.add((item.user_id, item.job, item.local_day))
    second = scheduler.due_jobs(NEW_YORK, _utc(2026, 11, 1, 6, 30), schedule, done)  # 01:30 EST

    assert [(d.user_id, d.job) for d in first] == [("ny", "coaching")]
    assert second == []


# ---------------------------------------------------------------------------
# Once per user per local day
# ---------------------------------------------------------------------------


def test_a_job_runs_once_per_local_day_then_again_the_next_day() -> None:
    users = [{"id": "u", "timezone": "Europe/London"}]
    first = scheduler.due_jobs(users, _utc(2026, 6, 15, 6, 0), SCHEDULE, set())
    assert [(d.job, d.local_day) for d in first] == [("coaching", "2026-06-15")]

    done = {(d.user_id, d.job, d.local_day) for d in first}
    assert scheduler.due_jobs(users, _utc(2026, 6, 15, 6, 30), SCHEDULE, done) == []

    next_day = scheduler.due_jobs(users, _utc(2026, 6, 16, 6, 0), SCHEDULE, done)
    assert [(d.job, d.local_day) for d in next_day] == [("coaching", "2026-06-16")]


def test_a_job_is_not_run_by_a_user_who_joins_long_after_the_slot() -> None:
    users = [{"id": "late", "timezone": "UTC"}]
    assert _due(users, _utc(2026, 6, 15, 7, 0) + timedelta(hours=3)) == set()


def test_weekly_insight_only_runs_on_the_users_local_sunday() -> None:
    users = [
        {"id": "tokyo", "timezone": "Asia/Tokyo"},
        {"id": "honolulu", "timezone": "Pacific/Honolulu"},
    ]
    # 23:00Z on Saturday 7 March is 08:00 Sunday in Tokyo but 13:00 Saturday in Honolulu.
    assert _due(users, _utc(2026, 3, 7, 23, 0)) == {("tokyo", "insight_weekly")}
    # The same wall-clock slot on Saturday in Tokyo is not a Sunday.
    assert ("tokyo", "insight_weekly") not in _due(users, _utc(2026, 3, 6, 23, 0))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class _Stop(Exception):
    pass


def _fake_clock(monkeypatch: pytest.MonkeyPatch, ticks: list[datetime]) -> None:
    """Make the scheduler's ``datetime.now`` walk through *ticks*, then stop."""
    state = {"index": 0}

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return ticks[min(state["index"], len(ticks) - 1)]

    def fake_sleep(_seconds: float) -> None:
        state["index"] += 1
        if state["index"] >= len(ticks):
            raise _Stop

    monkeypatch.setattr(scheduler, "datetime", FakeDateTime)
    monkeypatch.setattr(scheduler.time, "sleep", fake_sleep)


def test_run_schedule_dispatches_only_the_users_whose_local_slot_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users = [
        {"id": "london", "timezone": "Europe/London"},
        {"id": "new-york", "timezone": "America/New_York"},
    ]
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(scheduler, "get_all_users", lambda _db: users)
    monkeypatch.setattr(scheduler, "run_once", lambda _config: 0)
    monkeypatch.setattr(
        scheduler, "run_coaching", lambda _c, ids=None: calls.append(("coaching", tuple(ids or ())))
    )
    monkeypatch.setattr(
        scheduler,
        "run_daily_insight",
        lambda _c, ids=None: calls.append(("insight_daily", tuple(ids or ()))),
    )
    monkeypatch.setattr(
        scheduler,
        "run_weekly_correlations",
        lambda _c, ids=None: calls.append(("insight_weekly", tuple(ids or ()))),
    )
    # Boot on 14 June, then tick through 15 June (a Monday).
    _fake_clock(
        monkeypatch,
        [
            _utc(2026, 6, 14, 12, 0),  # boot cycle
            _utc(2026, 6, 15, 5, 0),  # London 06:00 BST -> daily insight
            _utc(2026, 6, 15, 6, 0),  # London 07:00 BST -> coaching
            _utc(2026, 6, 15, 6, 0),  # same minute again: must not repeat
            _utc(2026, 6, 15, 10, 0),  # New York 06:00 EDT -> daily insight
            _utc(2026, 6, 15, 11, 0),  # New York 07:00 EDT -> coaching
        ],
    )

    with pytest.raises(_Stop):
        scheduler.run_schedule(MagicMock(database_path="synthetic.db"))

    assert calls == [
        ("insight_daily", ("london",)),
        ("coaching", ("london",)),
        ("insight_daily", ("new-york",)),
        ("coaching", ("new-york",)),
    ]


def test_boot_cycle_marks_today_as_done_for_every_users_local_day() -> None:
    users = [
        {"id": "tokyo", "timezone": "Asia/Tokyo"},  # already Sunday 8 March
        {"id": "honolulu", "timezone": "Pacific/Honolulu"},  # still Saturday 7 March
    ]
    done: set[tuple[str, str, str]] = set()

    scheduler._mark_boot_cycle(users, _utc(2026, 3, 7, 23, 0), done)

    assert ("tokyo", "coaching", "2026-03-08") in done
    assert ("tokyo", "insight_weekly", "2026-03-08") in done  # Sunday there
    assert ("honolulu", "coaching", "2026-03-07") in done
    assert not any(key[0] == "honolulu" and key[1] == "insight_weekly" for key in done)


def test_run_once_sends_the_weekly_report_only_to_local_sunday_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users = [
        {"id": "tokyo", "timezone": "Asia/Tokyo"},
        {"id": "honolulu", "timezone": "Pacific/Honolulu"},
    ]
    weekly: list[tuple[str, ...]] = []
    monkeypatch.setattr(scheduler, "get_all_users", lambda _db: users)
    monkeypatch.setattr(scheduler, "run_coaching", lambda _c, ids=None: 0)
    monkeypatch.setattr(scheduler, "run_daily_insight", lambda _c, ids=None: None)
    monkeypatch.setattr(
        scheduler, "run_weekly_correlations", lambda _c, ids=None: weekly.append(tuple(ids or ()))
    )
    _fake_clock(monkeypatch, [_utc(2026, 3, 7, 23, 0)])  # Sunday in Tokyo only

    scheduler.run_once(MagicMock(database_path="synthetic.db"))

    assert weekly == [("tokyo",)]


def test_run_once_skips_the_weekly_report_when_nobody_is_on_sunday(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    weekly: list[Any] = []
    monkeypatch.setattr(scheduler, "get_all_users", lambda _db: [{"id": "u", "timezone": "UTC"}])
    monkeypatch.setattr(scheduler, "run_coaching", lambda _c, ids=None: 0)
    monkeypatch.setattr(scheduler, "run_daily_insight", lambda _c, ids=None: None)
    monkeypatch.setattr(scheduler, "run_weekly_correlations", lambda *a, **k: weekly.append(a))
    _fake_clock(monkeypatch, [_utc(2026, 3, 9, 12, 0)])  # a Monday

    scheduler.run_once(MagicMock(database_path="synthetic.db"))

    assert weekly == []


def test_per_user_filters_never_run_other_tenants_or_the_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = MagicMock(database_path="synthetic.db")
    users = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    monkeypatch.setattr(scheduler, "get_all_users", lambda _db: users)
    coached: list[str | None] = []

    def fake_run(*, preview: bool, user_id: str | None = None) -> int:
        coached.append(user_id)
        return 0

    monkeypatch.setattr("main.run", fake_run)

    assert scheduler.run_coaching(config, ["b"]) == 0
    assert scheduler.run_coaching(config, []) == 0  # nobody due: no legacy single-user run

    assert coached == ["b"]


def test_insight_jobs_accept_a_user_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    config = MagicMock(database_path="synthetic.db")
    monkeypatch.setattr(scheduler, "get_all_users", lambda _db: [{"id": "a"}, {"id": "b"}])
    daily: list[str | None] = []
    weekly: list[str | None] = []
    monkeypatch.setattr(
        "insight_cron.generate_daily_header", lambda _c, *, user_id=None: daily.append(user_id)
    )
    monkeypatch.setattr(
        "insight_cron.generate_weekly_correlations",
        lambda _c, *, user_id=None: weekly.append(user_id),
    )

    scheduler.run_daily_insight(config, ["a"])
    scheduler.run_weekly_correlations(config, ["b"])

    assert (daily, weekly) == (["a"], ["b"])
