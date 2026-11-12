"""Pure per-user regional rules: time zones, DST-safe local days, units, locales."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

import localisation as loc

NEW_YORK = "America/New_York"


def _utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Time zone validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["UTC", "Europe/London", "America/New_York", "America/Argentina/Buenos_Aires"],
)
def test_known_iana_zones_are_accepted(name: str) -> None:
    assert loc.normalise_timezone(name) == name


@pytest.mark.parametrize(
    "value",
    [
        "Mars/Base",
        "../etc/passwd",
        "/etc/localtime",
        "europe/london",  # IANA names are case sensitive
        " Europe/London",
        "Europe/London\n",
        "",
        "x" * 80,
        None,
        5,
        ["UTC"],
    ],
)
def test_unknown_or_hostile_zones_are_rejected(value: object) -> None:
    assert loc.normalise_timezone(value) is None
    assert loc.resolve_timezone(value) == loc.DEFAULT_TIMEZONE


def test_supported_zones_are_sorted_and_always_include_utc() -> None:
    zones = loc.supported_timezones()
    assert list(zones) == sorted(zones)
    assert "UTC" in zones and "Europe/London" in zones


# ---------------------------------------------------------------------------
# Instants and local days
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-03-02T09:00:00Z", _utc(2026, 3, 2, 9)),
        ("2026-03-02T09:00:00+00:00", _utc(2026, 3, 2, 9)),
        ("2026-03-02T23:30:00-05:00", _utc(2026, 3, 3, 4, 30)),
        ("2026-03-02T09:00:00", _utc(2026, 3, 2, 9)),  # naive means UTC
        (datetime(2026, 3, 2, 9, tzinfo=timezone(timedelta(hours=2))), _utc(2026, 3, 2, 7)),
    ],
)
def test_parse_instant_normalises_to_utc(value: object, expected: datetime) -> None:
    assert loc.parse_instant(value) == expected


@pytest.mark.parametrize("value", [None, "", "   ", "not a time", 12, "2026-13-45T00:00:00Z"])
def test_parse_instant_rejects_non_timestamps(value: object) -> None:
    assert loc.parse_instant(value) is None


def test_local_date_requires_a_timestamp() -> None:
    with pytest.raises(ValueError):
        loc.local_date("garbage", "UTC")


def test_same_instant_lands_on_different_local_days() -> None:
    instant = "2026-03-02T20:30:00+00:00"
    assert loc.local_date(instant, "UTC") == date(2026, 3, 2)
    assert loc.local_date(instant, "Pacific/Auckland") == date(2026, 3, 3)
    assert loc.local_date(instant, "America/Los_Angeles") == date(2026, 3, 2)


def test_unsupported_zone_falls_back_to_utc_days() -> None:
    assert loc.local_date("2026-03-02T23:30:00Z", "Mars/Base") == date(2026, 3, 2)


@pytest.mark.parametrize(
    ("instant", "local_day"),
    [
        # Spring forward, 2026-03-08: 02:00 EST jumps to 03:00 EDT at 07:00Z.
        (_utc(2026, 3, 8, 6, 59, 59), date(2026, 3, 8)),  # 01:59:59 EST
        (_utc(2026, 3, 8, 7, 0, 0), date(2026, 3, 8)),  # 03:00:00 EDT
        (_utc(2026, 3, 9, 3, 59, 59), date(2026, 3, 8)),  # 23:59:59 EDT
        # 00:30 EDT on the 9th. A fixed EST offset would wrongly say the 8th.
        (_utc(2026, 3, 9, 4, 30, 0), date(2026, 3, 9)),
        # Fall back, 2026-11-01: 02:00 EDT repeats as 01:00 EST at 06:00Z.
        (_utc(2026, 11, 1, 5, 30, 0), date(2026, 11, 1)),  # 01:30 EDT, first pass
        (_utc(2026, 11, 1, 6, 30, 0), date(2026, 11, 1)),  # 01:30 EST, second pass
        # 23:30 EST on the 1st. A fixed EDT offset would wrongly say the 2nd.
        (_utc(2026, 11, 2, 4, 30, 0), date(2026, 11, 1)),
        (_utc(2026, 11, 2, 5, 0, 0), date(2026, 11, 2)),  # 00:00 EST
    ],
)
def test_local_day_is_correct_across_new_york_dst_transitions(
    instant: datetime, local_day: date
) -> None:
    assert loc.local_date(instant, NEW_YORK) == local_day


@pytest.mark.parametrize(
    ("zone", "day", "hours"),
    [
        (NEW_YORK, date(2026, 3, 8), 23),  # spring forward
        (NEW_YORK, date(2026, 11, 1), 25),  # fall back
        (NEW_YORK, date(2026, 6, 15), 24),
        ("Europe/London", date(2026, 3, 29), 23),
        ("Europe/London", date(2026, 10, 25), 25),
        ("UTC", date(2026, 3, 8), 24),
        ("Asia/Kolkata", date(2026, 3, 8), 24),  # no DST at all
        ("Australia/Lord_Howe", date(2026, 4, 5), 24.5),  # 30-minute DST shift
        ("Australia/Lord_Howe", date(2026, 10, 4), 23.5),
        # Local midnight does not exist: DST starts at 00:00 and jumps to 01:00.
        ("America/Havana", date(2026, 3, 8), 23),
    ],
)
def test_local_day_length_follows_dst(zone: str, day: date, hours: float) -> None:
    assert loc.local_day_hours(day, zone) == hours


def test_local_day_bounds_are_half_open_and_utc() -> None:
    start, end = loc.local_day_bounds(date(2026, 3, 8), NEW_YORK)
    assert (start, end) == (_utc(2026, 3, 8, 5), _utc(2026, 3, 9, 4))
    assert start.utcoffset() == timedelta(0)
    assert loc.local_date(start, NEW_YORK) == date(2026, 3, 8)
    assert loc.local_date(end - timedelta(microseconds=1), NEW_YORK) == date(2026, 3, 8)
    assert loc.local_date(end, NEW_YORK) == date(2026, 3, 9)


def test_every_supported_zone_round_trips_sampled_local_days() -> None:
    """Bounds must map back to the same day for every zone on this host."""
    sample_days = [date(2026, month, day) for month in range(1, 13) for day in (1, 15)]
    for zone in loc.supported_timezones():
        for day in sample_days:
            start, end = loc.local_day_bounds(day, zone)
            assert loc.local_date(start, zone) == day, (zone, day)
            assert loc.local_date(end - timedelta(seconds=1), zone) == day, (zone, day)
            assert 22 <= (end - start).total_seconds() / 3600 <= 26, (zone, day)


# ---------------------------------------------------------------------------
# Deriving a stored workout day
# ---------------------------------------------------------------------------


def test_derive_local_day_keeps_the_source_instant_in_utc() -> None:
    assert loc.derive_local_day("2026-03-02T20:30:00+00:00", "Pacific/Auckland") == (
        "2026-03-03",
        "2026-03-02T20:30:00+00:00",
    )
    assert loc.derive_local_day("2026-03-02T23:30:00-05:00", "UTC") == (
        "2026-03-03",
        "2026-03-03T04:30:00+00:00",
    )


def test_derive_local_day_leaves_bare_calendar_days_alone() -> None:
    assert loc.derive_local_day("2026-03-02", "Pacific/Auckland") == ("2026-03-02", None)


def test_derive_local_day_keeps_legacy_behaviour_for_unparseable_values() -> None:
    assert loc.derive_local_day("not-a-timestamp", "UTC") == ("not-a-time", None)
    assert loc.derive_local_day(None, "UTC") == (None, None)
    assert loc.derive_local_day("", "UTC") == (None, None)


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def test_pound_is_exactly_the_international_avoirdupois_pound() -> None:
    assert loc.KG_PER_LB == 0.45359237
    assert loc.kg_to_lb(0.45359237) == pytest.approx(1.0)
    assert loc.lb_to_kg(1) == pytest.approx(0.45359237)


@pytest.mark.parametrize(
    ("kilograms", "pounds"),
    [(0, 0), (1, 2.2), (2.5, 5.5), (20, 44.1), (60, 132.3), (100, 220.5), (142.5, 314.2)],
)
def test_imperial_display_rounds_to_a_tenth_of_a_pound(
    kilograms: float, pounds: float
) -> None:
    assert loc.convert_weight(kilograms, "imperial") == pounds


@pytest.mark.parametrize("kilograms", [0, 2.5, 81.45, 100, 142.5])
def test_metric_display_never_alters_the_stored_value(kilograms: float) -> None:
    assert loc.convert_weight(kilograms, "metric") == kilograms


def test_missing_weight_stays_missing() -> None:
    assert loc.convert_weight(None, "imperial") is None
    assert loc.format_weight(None, "metric") == "-"


@pytest.mark.parametrize(
    ("kilograms", "system", "text"),
    [
        (110.0, "metric", "110 kg"),
        (82.5, "metric", "82.5 kg"),
        (81.45, "metric", "81.45 kg"),  # metric keeps full precision
        (100.0, "imperial", "220.5 lb"),
        (60, "imperial", "132.3 lb"),
        (0.45359237, "imperial", "1 lb"),
    ],
)
def test_format_weight(kilograms: float, system: str, text: str) -> None:
    assert loc.format_weight(kilograms, system) == text


def test_typed_imperial_weights_convert_back_to_kilograms() -> None:
    assert loc.weight_input_to_kg(225, "imperial") == 102.058
    assert loc.weight_input_to_kg(102.5, "metric") == 102.5


@pytest.mark.parametrize("kilograms", [20.0, 60.0, 100.0, 142.5, 200.0])
def test_round_trip_error_stays_inside_display_rounding(kilograms: float) -> None:
    shown = loc.convert_weight(kilograms, "imperial")
    assert shown is not None
    # 0.05 lb is the largest error rounding to 0.1 lb can introduce.
    assert abs(loc.weight_input_to_kg(shown, "imperial") - kilograms) <= 0.05 * loc.KG_PER_LB


def test_unit_labels_and_load_steps() -> None:
    assert (loc.weight_unit("metric"), loc.load_step("metric")) == ("kg", "2.5 kg")
    assert (loc.weight_unit("imperial"), loc.load_step("imperial")) == ("lb", "5 lb")


@pytest.mark.parametrize("value", ["stone", "", None, 3])
def test_unsupported_unit_systems_are_rejected_and_fall_back_to_metric(value: object) -> None:
    assert loc.normalise_unit_system(value) is None
    assert loc.resolve_unit_system(value) == "metric"
    assert loc.weight_unit(value) == "kg"


def test_unit_systems_ignore_case_and_surrounding_whitespace() -> None:
    assert loc.normalise_unit_system(" IMPERIAL ") == "imperial"
    assert loc.weight_unit(" Imperial") == "lb"


# ---------------------------------------------------------------------------
# Locales
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "tag"),
    [("en-GB", "en-GB"), ("en-gb", "en-GB"), ("en_US", "en-US"), (" EN-au ", "en-AU")],
)
def test_supported_locales_are_canonicalised(value: str, tag: str) -> None:
    assert loc.normalise_locale(value) == tag


@pytest.mark.parametrize(
    "value", ["fr-FR", "de-DE", "xx", "en", "en-ZZ", "tlh", "", None, 7, "en-GB;q=0.9"]
)
def test_unsupported_locales_are_rejected_and_fall_back(value: object) -> None:
    assert loc.normalise_locale(value) is None
    assert loc.resolve_locale(value) == loc.DEFAULT_LOCALE
    assert loc.spelling_for(value) == "British English"


def test_every_supported_locale_has_a_label_and_spelling() -> None:
    assert loc.DEFAULT_LOCALE in loc.SUPPORTED_LOCALES
    for tag, (label, spelling) in loc.LOCALE_PROFILES.items():
        assert label and spelling and tag.startswith("en-")
    assert loc.spelling_for("en-US") == "American English"


def test_explicit_defaults() -> None:
    assert (loc.DEFAULT_TIMEZONE, loc.DEFAULT_LOCALE, loc.DEFAULT_UNIT_SYSTEM) == (
        "UTC",
        "en-GB",
        "metric",
    )


# ---------------------------------------------------------------------------
# AI prompt context
# ---------------------------------------------------------------------------


def test_prompt_context_uses_the_users_local_date_across_dst() -> None:
    # 2026-03-09T04:30Z is already Monday the 9th in New York (EDT).
    text = loc.prompt_context(NEW_YORK, "en-US", "metric", now=_utc(2026, 3, 9, 4, 30))
    assert "Time zone: America/New_York" in text
    assert "Monday 2026-03-09" in text
    assert "American English" in text

    # The same instant is still Sunday the 8th in Los Angeles.
    other = loc.prompt_context(
        "America/Los_Angeles", "en-US", "metric", now=_utc(2026, 3, 9, 4, 30)
    )
    assert "Sunday 2026-03-08" in other


def test_prompt_context_tells_the_model_how_to_present_weights() -> None:
    now = _utc(2026, 3, 2, 12)
    imperial = loc.prompt_context("UTC", "en-GB", "imperial", now=now)
    assert "pounds" in imperial and "never show kilograms" in imperial
    assert "stored in kilograms" in imperial
    metric = loc.prompt_context("UTC", "en-GB", "metric", now=now)
    assert "kilograms (kg)" in metric and "pounds" not in metric


def test_prompt_context_falls_back_for_corrupt_stored_values() -> None:
    text = loc.prompt_context("Mars/Base", "fr-FR", "stone", now=_utc(2026, 3, 2, 12))
    assert "Time zone: UTC" in text
    assert "Locale: en-GB" in text
    assert "British English" in text
    assert "Units: metric" in text
