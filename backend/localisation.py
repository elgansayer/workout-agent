"""Pure per-user regional rules: local days, DST-safe bounds, units and prompts.

Nothing here touches the database or the network. The contract every caller
relies on:

* Source timestamps are stored as UTC instants and are never rewritten.
* A user's time zone only changes *derived* local-day groupings, which are
  recomputed from those instants (see ``user_settings.recompute_local_days``).
* Weights are stored in kilograms. The unit system only changes how a value is
  displayed or typed, never what is persisted.
* Unsupported or corrupted stored values fall back to the explicit defaults
  below instead of raising in a page, job or notification.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from types import MappingProxyType
from zoneinfo import available_timezones

from programme_runtime import local_today, user_timezone

DEFAULT_TIMEZONE = "UTC"
DEFAULT_LOCALE = "en-GB"
DEFAULT_UNIT_SYSTEM = "metric"

UNIT_SYSTEMS: tuple[str, ...] = ("metric", "imperial")

# Locales the product can genuinely serve: the interface copy is English, so
# the supported set is the English regional variants whose date/number
# conventions and spelling we can honour. Anything else is rejected on write
# and falls back to DEFAULT_LOCALE on read. Value: (menu label, spelling).
LOCALE_PROFILES: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "en-GB": ("English (United Kingdom)", "British English"),
        "en-IE": ("English (Ireland)", "British English"),
        "en-US": ("English (United States)", "American English"),
        "en-CA": ("English (Canada)", "Canadian English"),
        "en-AU": ("English (Australia)", "Australian English"),
        "en-NZ": ("English (New Zealand)", "New Zealand English"),
    }
)
SUPPORTED_LOCALES: tuple[str, ...] = tuple(LOCALE_PROFILES)

# The international avoirdupois pound is defined as exactly 0.45359237 kg.
KG_PER_LB = 0.45359237

# IANA names have at most three segments (e.g. America/Argentina/Buenos_Aires).
_TIMEZONE_NAME = re.compile(r"[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+){0,2}")
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")


# ---------------------------------------------------------------------------
# Locale
# ---------------------------------------------------------------------------


def normalise_locale(value: object) -> str | None:
    """Return the canonical supported tag for *value*, or ``None``."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().replace("_", "-").lower()
    for tag in LOCALE_PROFILES:
        if tag.lower() == candidate:
            return tag
    return None


def resolve_locale(value: object) -> str:
    """Return a supported locale, falling back to ``DEFAULT_LOCALE``."""
    return normalise_locale(value) or DEFAULT_LOCALE


def spelling_for(locale: object) -> str:
    """Return the English variant used when writing for *locale*."""
    return LOCALE_PROFILES[resolve_locale(locale)][1]


# ---------------------------------------------------------------------------
# Time zones and local days
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def supported_timezones() -> tuple[str, ...]:
    """Every IANA zone this host can resolve, sorted, always including UTC."""
    return tuple(sorted({*available_timezones(), DEFAULT_TIMEZONE}))


@lru_cache(maxsize=1)
def _timezone_set() -> frozenset[str]:
    return frozenset(supported_timezones())


def normalise_timezone(value: object) -> str | None:
    """Return *value* when it is a known IANA zone name, otherwise ``None``."""
    if (
        isinstance(value, str)
        and len(value) <= 64
        and _TIMEZONE_NAME.fullmatch(value)
        and value in _timezone_set()
    ):
        return value
    return None


def resolve_timezone(value: object) -> str:
    """Return a known zone name, falling back to ``DEFAULT_TIMEZONE``."""
    return normalise_timezone(value) or DEFAULT_TIMEZONE


def parse_instant(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC instant.

    Timestamps without an offset are interpreted as UTC, matching how the
    programme runtime reads stored workout timestamps. Unparseable values and
    non-timestamps return ``None``.
    """
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def local_date(instant: object, zone_name: object) -> date:
    """Return the calendar day of *instant* in the user's time zone."""
    parsed = parse_instant(instant)
    if parsed is None:
        raise ValueError("a timezone-aware timestamp is required")
    return parsed.astimezone(user_timezone(resolve_timezone(zone_name))).date()


def local_day_bounds(day: date, zone_name: object) -> tuple[datetime, datetime]:
    """Return the UTC ``[start, end)`` instants that bound a local calendar day.

    The span is 23, 24 or 25 hours depending on daylight-saving transitions, so
    callers must never assume a fixed 24-hour day.
    """
    zone = user_timezone(resolve_timezone(zone_name))
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def local_day_hours(day: date, zone_name: object) -> float:
    """Return how many real hours the local calendar day lasts."""
    start, end = local_day_bounds(day, zone_name)
    return (end - start).total_seconds() / 3600


def derive_local_day(
    value: str | None, zone_name: object
) -> tuple[str | None, str | None]:
    """Split a stored workout date into ``(local_day, utc_instant)``.

    * a full timestamp yields its local day in *zone_name* plus the canonical
      UTC instant, which is the source value to persist;
    * a bare ``YYYY-MM-DD`` is already a calendar day, so it is kept verbatim
      with no instant (there is nothing to recompute it from);
    * anything unparseable keeps the historical ``value[:10]`` behaviour.
    """
    if not value:
        return None, None
    text = value.strip()
    if _DATE_ONLY.fullmatch(text):
        return text, None
    instant = parse_instant(text)
    if instant is None:
        return text[:10], None
    return local_date(instant, zone_name).isoformat(), instant.isoformat()


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def normalise_unit_system(value: object) -> str | None:
    """Return ``metric`` or ``imperial`` for *value*, otherwise ``None``."""
    if isinstance(value, str) and value.strip().lower() in UNIT_SYSTEMS:
        return value.strip().lower()
    return None


def resolve_unit_system(value: object) -> str:
    """Return a supported unit system, falling back to ``DEFAULT_UNIT_SYSTEM``."""
    return normalise_unit_system(value) or DEFAULT_UNIT_SYSTEM


def kg_to_lb(kilograms: float) -> float:
    return kilograms / KG_PER_LB


def lb_to_kg(pounds: float) -> float:
    return pounds * KG_PER_LB


def weight_unit(unit_system: object) -> str:
    """Return the display label for the user's weight unit."""
    return "lb" if resolve_unit_system(unit_system) == "imperial" else "kg"


def convert_weight(kilograms: float | None, unit_system: object) -> float | None:
    """Convert a stored kilogram value for display.

    Metric values are returned untouched so existing output stays identical;
    imperial values are rounded to 0.1 lb, which is finer than any plate.
    """
    if kilograms is None:
        return None
    if resolve_unit_system(unit_system) == "imperial":
        return round(kg_to_lb(float(kilograms)), 1)
    return kilograms


def format_weight(kilograms: float | None, unit_system: object) -> str:
    """Format a stored kilogram value, e.g. ``100 kg`` or ``220.5 lb``."""
    converted = convert_weight(kilograms, unit_system)
    if converted is None:
        return "-"
    return f"{converted:g} {weight_unit(unit_system)}"


def weight_input_to_kg(value: float, unit_system: object) -> float:
    """Convert a value the user typed in their own unit back to kilograms."""
    if resolve_unit_system(unit_system) == "imperial":
        return round(lb_to_kg(float(value)), 3)
    return float(value)


def load_step(unit_system: object) -> str:
    """Return the smallest practical load increase in the user's unit."""
    return "5 lb" if resolve_unit_system(unit_system) == "imperial" else "2.5 kg"


# ---------------------------------------------------------------------------
# AI prompts
# ---------------------------------------------------------------------------


def prompt_context(
    timezone_name: object,
    locale: object,
    unit_system: object,
    *,
    now: datetime | None = None,
) -> str:
    """Describe the user's regional preferences for an AI prompt.

    The text only tells the model how to present validated data. It never
    changes the data itself: stored values stay in kilograms and UTC.
    """
    zone = resolve_timezone(timezone_name)
    moment = now or datetime.now(timezone.utc)
    today = local_today(zone, moment)
    units = resolve_unit_system(unit_system)
    if units == "imperial":
        unit_line = (
            "Units: imperial. Every weight in the data is stored in kilograms "
            "(fields ending in _kg). Convert to pounds (1 kg = 2.2046 lb, "
            "rounded to the nearest 0.5 lb) before quoting any load or body "
            "weight, and never show kilograms."
        )
    else:
        unit_line = (
            "Units: metric. Quote every weight in kilograms (kg), as stored."
        )
    return (
        "Regional preferences for this user (apply to your whole reply):\n"
        f"- Time zone: {zone}. Their local date today is "
        f"{today.strftime('%A')} {today.isoformat()}.\n"
        f"- Locale: {resolve_locale(locale)}. Write in {spelling_for(locale)} "
        "and write dates unambiguously (for example 8 March 2026).\n"
        f"- {unit_line}"
    )


__all__ = [
    "DEFAULT_LOCALE",
    "DEFAULT_TIMEZONE",
    "DEFAULT_UNIT_SYSTEM",
    "KG_PER_LB",
    "LOCALE_PROFILES",
    "SUPPORTED_LOCALES",
    "UNIT_SYSTEMS",
    "convert_weight",
    "derive_local_day",
    "format_weight",
    "kg_to_lb",
    "lb_to_kg",
    "load_step",
    "local_date",
    "local_day_bounds",
    "local_day_hours",
    "local_today",
    "normalise_locale",
    "normalise_timezone",
    "normalise_unit_system",
    "parse_instant",
    "prompt_context",
    "resolve_locale",
    "resolve_timezone",
    "resolve_unit_system",
    "spelling_for",
    "supported_timezones",
    "weight_input_to_kg",
    "weight_unit",
]
