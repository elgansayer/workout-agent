"""Internal progressive-overload web app (FastAPI).

A self-hosted dashboard for the workout agent. It reads the same SQLite database
the agent writes and renders it as a rich, read-only control centre: today's
session and progressive-overload targets, server-rendered SVG charts of every
lift and your body composition, all-time personal records, training-load trends,
a consistency calendar, the full periodisation plan, and programme check-ins.

There is no login: it is read-only and meant to sit behind a reverse proxy on a
trusted host (e.g. Apache -> Docker -> gym.example.com). All motivation is
automated; nothing here calls out to an API on a page view.

Run locally:   uvicorn webapp.app:app --reload
In a container: see Dockerfile.web / the `web` service in docker-compose.yml
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from database import (
    get_body_metrics,
    get_checkins,
    get_daily_logs,
    get_exercise_volumes,
    get_personal_records,
    get_programme_start_date,
    get_progress_history,
    get_recent_bests,
    get_session_volumes,
    init_db,
)
import analytics
import lifestyle
from webapp import charts
from hevy_parser import normalise_name
from program import (
    BLOCKS,
    BLOCK_WEEKS,
    COACHING_RULES,
    CYCLE_WEEKS,
    SPLIT_NAME,
    block_for_week,
    day_exercises,
    day_focus,
    today_day,
    week_in_cycle,
)

DB_PATH = os.environ.get("DATABASE_PATH", "workout_agent.db").strip()

_BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Safe even if the agent already created the database (CREATE IF NOT EXISTS).
    init_db(DB_PATH)
    yield


app = FastAPI(title="Workout Agent", docs_url=None, redoc_url=None, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(_BASE_DIR / "static")), name="static")


# Automated hype lines. One is shown per day, chosen deterministically from the
# date, so the dashboard always greets you without an API call or a button.
_QUOTES = [
    "Show up today and the bar gets lighter tomorrow. One more rep than last time.",
    "Strength is a skill you practise. Brace, pull, repeat.",
    "Small jumps, stacked weekly, become a competition physique. Keep stacking.",
    "Your future deadlift PR is built on the boring set you log today.",
    "Discipline now, six-pack later. Hit your protein and your top set.",
    "The last clean rep is where the growth lives. Earn it.",
    "Win the morning weigh-in by winning last night's sleep.",
    "Tight waist, wide back, heavy bar. Stay the course.",
    "You do not need motivation, you need the next set. Go.",
    "Consistency is the cheat code. You already have it.",
    "Add a kilo or add a rep. Either way, move forward.",
    "Recovery is training too. Eat, sleep, repeat.",
]


def _daily_quote(today: date | None = None) -> str:
    today = today or date.today()
    return _QUOTES[today.toordinal() % len(_QUOTES)]


def _epley_1rm(weight: float | None, reps: int | None) -> float | None:
    return analytics.epley_1rm(weight, reps)


def _weight_on_or_before(weights: list[tuple[str, float]], when: str) -> float | None:
    """Latest bodyweight recorded on or before a date (weights sorted ascending)."""
    result = None
    for when_w, value in weights:
        if when_w <= when:
            result = value
        else:
            break
    return result


def _find_lift_series(series: dict, *keywords: str) -> tuple[str | None, list]:
    """Find an exercise whose name contains all (then any) of the keywords."""
    for name, entries in series.items():
        low = name.lower()
        if all(k in low for k in keywords):
            return name, entries
    for name, entries in series.items():
        low = name.lower()
        if any(k in low for k in keywords):
            return name, entries
    return None, []


def _rep_top(rep_range: str) -> int | None:
    digits = [int(n) for n in rep_range.replace("-", " ").split() if n.isdigit()]
    return digits[-1] if digits else None


def _overload_nudge(planned_rep_range: str, best: dict | None) -> str:
    """Suggest the next progressive-overload step for an exercise."""
    if not best or best.get("top_reps") is None:
        return "Log this lift to start tracking progress."
    top = _rep_top(planned_rep_range)
    reps = best["top_reps"]
    weight = best.get("top_weight_kg")
    if top is not None and reps >= top:
        if weight:
            return "You hit the top of the range. Add 2.5 kg next time."
        return "You hit the top of the range. Add a rep or a little load."
    if top is not None:
        return f"Chase {top} clean reps, then add load."
    return "Keep the reps strict and progress when it feels easy."


def _format_best(best: dict | None) -> str:
    if not best:
        return "No data yet"
    weight = best.get("top_weight_kg")
    reps = best.get("top_reps")
    if weight is not None and reps is not None:
        return f"{weight:g} kg x {reps}"
    if reps is not None:
        return f"{reps} reps"
    return "No data yet"


def _training_levels() -> dict[str, int]:
    """Map ISO dates to a calendar-heatmap intensity (0-4)."""
    levels: dict[str, int] = {}
    for log in get_daily_logs(limit=400, db_path=DB_PATH):
        levels[log["date"]] = 2 if log["day"] is not None else 1
    # A session with logged sets is the strongest signal of a completed workout.
    for session in get_session_volumes(db_path=DB_PATH):
        levels[session["date"]] = 4
    return levels


def _current_streak(levels: dict[str, int]) -> int:
    """Count consecutive recent days that had any logged activity."""
    streak = 0
    day = date.today()
    # Allow today to be empty (the morning run may not have happened yet).
    if levels.get(day.isoformat(), 0) == 0:
        day -= timedelta(days=1)
    while levels.get(day.isoformat(), 0) > 0:
        streak += 1
        day -= timedelta(days=1)
    return streak


def _dashboard_context(today: date | None = None) -> dict:
    if today is None:
        today = date.today()
    start = get_programme_start_date(DB_PATH)
    week = week_in_cycle(start, today)
    block = block_for_week(week)
    bests = get_recent_bests(DB_PATH)
    bests_norm = {normalise_name(name): best for name, best in bests.items()}

    day = today_day(today)
    rows: list[dict] = []
    focus = "Rest & Recovery"
    if day is not None:
        focus = day_focus(day)
        for ex in day_exercises(day, block):
            best = bests_norm.get(normalise_name(ex.name))
            rows.append(
                {
                    "name": ex.name,
                    "planned": f"{ex.sets} x {ex.rep_range}",
                    "note": ex.note,
                    "last": _format_best(best),
                    "nudge": _overload_nudge(ex.rep_range, best),
                }
            )

    metrics = get_body_metrics(db_path=DB_PATH)
    latest_weight = metrics[-1]["weight_kg"] if metrics else None
    recovery_like = {"weight_kg": latest_weight} if latest_weight else None
    guidance = lifestyle.daily_guidance(day, day is None, recovery_like)

    levels = _training_levels()
    streak = _current_streak(levels)
    week_in_block = ((week - 1) % BLOCK_WEEKS) + 1

    weight_spark = charts.sparkline([m["weight_kg"] for m in metrics if m["weight_kg"]])
    fat_spark = charts.sparkline(
        [m["body_fat_pct"] for m in metrics if m["body_fat_pct"]], colour=charts.WARN
    )
    latest_fat = next(
        (m["body_fat_pct"] for m in reversed(metrics) if m["body_fat_pct"]), None
    )

    return {
        "active": "today",
        "quote": _daily_quote(today),
        "week": week,
        "cycle_weeks": CYCLE_WEEKS,
        "block": block,
        "week_in_block": week_in_block,
        "block_weeks": BLOCK_WEEKS,
        "focus": focus,
        "is_rest_day": day is None,
        "weekday": today.strftime("%A"),
        "rows": rows,
        "lifestyle": guidance.as_lines(),
        "cycle_ring": charts.progress_ring(
            week / CYCLE_WEEKS * 100, label=f"Wk {week}", sub=f"of {CYCLE_WEEKS}"
        ),
        "block_ring": charts.progress_ring(
            week_in_block / BLOCK_WEEKS * 100,
            label=block.name.split(" ")[0],
            sub=f"wk {week_in_block}/{BLOCK_WEEKS}",
            colour=charts.ACCENT_2,
        ),
        "streak": streak,
        "calendar": charts.calendar_heatmap(levels),
        "weight": f"{latest_weight:g} kg" if latest_weight else None,
        "weight_spark": weight_spark,
        "body_fat": f"{latest_fat:g} %" if latest_fat else None,
        "fat_spark": fat_spark,
    }


@app.get("/")
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", _dashboard_context())


@app.get("/progress")
def progress(request: Request):
    series = get_progress_history(db_path=DB_PATH)
    charts_data = []
    for name in sorted(series):
        entries = series[name]
        points = [
            {
                "date": e["date"][5:],
                "value": e["top_weight_kg"],
                "label": _format_best(e),
            }
            for e in entries
        ]
        e1rms = [_epley_1rm(e["top_weight_kg"], e["top_reps"]) for e in entries]
        e1rms = [v for v in e1rms if v]
        best_e1rm = max(e1rms) if e1rms else None
        charts_data.append(
            {
                "name": name,
                "svg": charts.line_chart(points, unit="kg"),
                "best_e1rm": best_e1rm,
                "sessions": len(entries),
            }
        )
    return templates.TemplateResponse(
        request,
        "progress.html",
        {"active": "progress", "charts": charts_data, "body": _body_charts()},
    )


def _body_charts() -> dict:
    readings = get_body_metrics(db_path=DB_PATH)

    def _series(key: str, unit: str, colour: str) -> str | None:
        points = [
            {"date": r["date"][5:], "value": r[key], "label": f"{r[key]:g} {unit}"}
            for r in readings
            if r[key] is not None
        ]
        if len(points) < 2:
            return None
        return charts.line_chart(points, unit=unit, colour=colour)

    return {
        "weight": _series("weight_kg", "kg", charts.ACCENT),
        "body_fat": _series("body_fat_pct", "%", charts.WARN),
        "muscle": _series("muscle_pct", "%", charts.ACCENT_2),
        "resting_hr": _series("resting_hr", "bpm", charts.PINK),
    }


@app.get("/stats")
def stats(request: Request):
    volumes = get_session_volumes(db_path=DB_PATH)
    prs = get_personal_records(db_path=DB_PATH)
    logs = get_daily_logs(limit=400, db_path=DB_PATH)
    start = get_programme_start_date(DB_PATH)
    series = get_progress_history(db_path=DB_PATH)
    today = date.today()
    week = week_in_cycle(start, today)

    # Headline totals.
    total_sessions = len(volumes)
    total_volume = sum(v["volume"] for v in volumes)
    days_on_programme = (today - start).days

    # Training split distribution (donut) from the daily log.
    focus_counts: dict[str, int] = {}
    for log in logs:
        if log["day"] is not None:
            focus_counts[log["focus"]] = focus_counts.get(log["focus"], 0) + 1
    distribution = charts.donut(
        [{"label": k, "value": v} for k, v in sorted(focus_counts.items())]
    )

    # Volume broken down by muscle group.
    groups = analytics.group_volumes(get_exercise_volumes(db_path=DB_PATH))
    muscle_donut = charts.donut(
        [{"label": g, "value": v} for g, v in sorted(groups.items(), key=lambda kv: -kv[1])]
    )

    # Session-load trend over the most recent sessions.
    recent = volumes[-14:]
    volume_bars = charts.bar_chart(
        [{"label": v["date"][5:], "value": v["volume"], "caption": v["date"]} for v in recent],
        unit="kg",
    )

    # Strength score (DOTS) and strength-to-bodyweight ratio over time, built
    # from the deadlift e1RM against the bodyweight recorded at the time.
    body = get_body_metrics(db_path=DB_PATH)
    weights = [(m["date"], m["weight_kg"]) for m in body if m["weight_kg"]]
    _, dl_entries = _find_lift_series(series, "deadlift")
    dots_points, ratio_points = [], []
    for e in dl_entries:
        e1rm = analytics.epley_1rm(e["top_weight_kg"], e["top_reps"])
        bw = _weight_on_or_before(weights, e["date"])
        if not e1rm or not bw:
            continue
        score = analytics.dots_score(bw, e1rm)
        if score:
            dots_points.append({"date": e["date"][5:], "value": score, "label": f"{score:g}"})
        ratio_points.append(
            {"date": e["date"][5:], "value": round(e1rm / bw, 2), "label": f"{e1rm / bw:.2f}x"}
        )
    dots_chart = charts.line_chart(dots_points, colour=charts.PURPLE) if len(dots_points) > 1 else None
    ratio_chart = charts.line_chart(ratio_points, unit="x", colour=charts.ACCENT_2) if len(ratio_points) > 1 else None

    # Project the main lifts to the end of the current 12-week cycle.
    target_ordinal = today.toordinal() + max(0, (CYCLE_WEEKS - week)) * 7
    projections = [
        _project_lift("Deadlift", dl_entries, target_ordinal, metric="e1rm"),
        _project_lift("Pull-ups", _find_lift_series(series, "pull")[1], target_ordinal),
    ]
    projections = [p for p in projections if p]

    pr_rows = [
        {
            "exercise": pr["exercise"],
            "e1rm": round(pr["e1rm"], 1),
            "detail": f"{pr['weight_kg']:g} kg x {pr['reps']}",
            "date": pr["date"],
        }
        for pr in prs
    ]

    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "active": "stats",
            "total_sessions": total_sessions,
            "total_volume": round(total_volume),
            "days_on_programme": days_on_programme,
            "exercises_tracked": len(prs),
            "distribution": distribution,
            "has_distribution": bool(focus_counts),
            "muscle_donut": muscle_donut,
            "has_muscle": bool(groups),
            "volume_bars": volume_bars,
            "has_volume": bool(recent),
            "dots_chart": dots_chart,
            "ratio_chart": ratio_chart,
            "projections": projections,
            "prs": pr_rows,
        },
    )


def _project_lift(
    label: str, entries: list, target_ordinal: int, *, metric: str = "auto"
) -> dict | None:
    """Build a projection card for a lift at the end of the cycle."""
    points: list[tuple[float, float]] = []
    unit = "kg"
    use_e1rm = metric == "e1rm"
    if metric == "auto":
        use_e1rm = any(e["top_weight_kg"] for e in entries)
    for e in entries:
        value = (
            analytics.epley_1rm(e["top_weight_kg"], e["top_reps"])
            if use_e1rm
            else e["top_reps"]
        )
        if value is None:
            continue
        points.append((float(date.fromisoformat(e["date"]).toordinal()), float(value)))
    if len(points) < 2:
        return None
    if not use_e1rm:
        unit = "reps"
    current = points[-1][1]
    projected = analytics.project(points, target_ordinal)
    if projected is None:
        return None
    projected = max(projected, current)  # never project a regression below today
    return {
        "label": label,
        "current": f"{current:g} {unit}",
        "projected": f"{projected:g} {unit}",
        "metric": "est. 1RM" if use_e1rm else "top reps",
        "gain": round(projected - current, 1),
    }



@app.get("/plan")
def plan(request: Request):
    today = date.today()
    week = week_in_cycle(get_programme_start_date(DB_PATH), today)
    current_block = block_for_week(week)
    day = today_day(today)

    days = []
    for d in range(1, 7):
        days.append(
            {
                "number": d,
                "focus": day_focus(d),
                "is_today": d == day,
                "exercises": [
                    {"name": ex.name, "scheme": f"{ex.sets} x {ex.rep_range}", "note": ex.note}
                    for ex in day_exercises(d, current_block)
                ],
            }
        )

    blocks = [
        {
            "number": b.number,
            "name": b.name,
            "weeks": b.weeks,
            "focus": b.focus,
            "deadlift": f"{b.deadlift.sets} x {b.deadlift.rep_range}",
            "pullups": f"{b.pullups.sets} x {b.pullups.rep_range}",
            "accessory": b.accessory_emphasis,
            "is_current": b.number == current_block.number,
        }
        for b in BLOCKS.values()
    ]

    return templates.TemplateResponse(
        request,
        "plan.html",
        {
            "active": "plan",
            "split_name": SPLIT_NAME,
            "week": week,
            "cycle_weeks": CYCLE_WEEKS,
            "current_block": current_block,
            "blocks": blocks,
            "days": days,
            "rules": COACHING_RULES,
        },
    )


@app.get("/history")
def history(request: Request):
    logs = get_daily_logs(limit=60, db_path=DB_PATH)
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "active": "history",
            "calendar": charts.calendar_heatmap(_training_levels()),
            "logs": logs,
        },
    )


@app.get("/checkins")
def checkins(request: Request):
    return templates.TemplateResponse(
        request,
        "checkins.html",
        {"active": "checkins", "checkins": get_checkins(db_path=DB_PATH)},
    )


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    """Serve the service worker from the root so it can control every page.

    A worker only controls URLs within its own path, so it must be served from
    "/" (with the Service-Worker-Allowed header) rather than from /static.
    """
    return FileResponse(
        _BASE_DIR / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )
