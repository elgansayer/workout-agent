"""Dependency-free, server-rendered SVG charts.

Everything here returns a self-contained SVG string. No JavaScript and no
external chart library, so the dashboard renders instantly, works completely
offline behind a reverse proxy, and prints crisply. Charts scale to their
container via a viewBox and ``width: 100%`` in the stylesheet.

All caller-supplied text is escaped, so exercise names from Hevy are safe to
drop straight into the markup.
"""

from __future__ import annotations

import html
from datetime import date, timedelta
from typing import Sequence

# House palette (kept in step with style.css custom properties).
ACCENT = "#4ade80"
ACCENT_2 = "#38bdf8"
WARN = "#f59e0b"
PINK = "#f472b6"
PURPLE = "#a78bfa"
GRID = "#262c37"
MUTED = "#9aa3b2"
TRACK = "#1e232c"

SERIES_COLOURS = [ACCENT, ACCENT_2, WARN, PINK, PURPLE, "#34d399", "#fbbf24"]


def _esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def _fmt(value: float) -> str:
    """Trim trailing zeros so 80.0 -> '80' and 80.5 -> '80.5'."""
    return f"{value:g}"


def _nice_round(value: float) -> str:
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return _fmt(round(value, 1))


def line_chart(
    points: Sequence[dict],
    *,
    colour: str = ACCENT,
    unit: str = "",
    height: int = 220,
    width: int = 720,
) -> str:
    """A smooth area + line chart.

    ``points`` is a list of ``{"date": str, "value": float, "label": str}``.
    """
    pts = [p for p in points if p.get("value") is not None]
    if len(pts) < 2:
        return _empty_chart("Not enough data yet", width, height)

    pad_l, pad_r, pad_t, pad_b = 48, 16, 18, 28
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    values = [float(p["value"]) for p in pts]
    lo, hi = min(values), max(values)
    if hi == lo:
        hi += 1
        lo -= 1
    span = hi - lo

    def x(i: int) -> float:
        return pad_l + (i / (len(pts) - 1)) * plot_w

    def y(v: float) -> float:
        return pad_t + (1 - (v - lo) / span) * plot_h

    coords = [(x(i), y(v)) for i, v in enumerate(values)]
    line_pts = " ".join(f"{cx:.1f},{cy:.1f}" for cx, cy in coords)
    area_pts = (
        f"{coords[0][0]:.1f},{pad_t + plot_h:.1f} "
        + line_pts
        + f" {coords[-1][0]:.1f},{pad_t + plot_h:.1f}"
    )

    grid = []
    label_axis = []
    for frac in (0.0, 0.5, 1.0):
        gv = lo + span * frac
        gy = y(gv)
        grid.append(
            f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width - pad_r}" y2="{gy:.1f}" '
            f'stroke="{GRID}" stroke-width="1"/>'
        )
        label_axis.append(
            f'<text x="{pad_l - 6}" y="{gy + 3:.1f}" text-anchor="end" '
            f'class="svg-axis">{_nice_round(gv)}</text>'
        )

    first_date = _esc(pts[0]["date"])
    last_date = _esc(pts[-1]["date"])
    last_label = _esc(pts[-1].get("label") or f"{_fmt(values[-1])} {unit}".strip())
    lx, ly = coords[-1]

    dots = "".join(
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="2.4" fill="{colour}"/>'
        for cx, cy in coords
    )

    grad_id = f"grad{abs(hash(line_pts)) % 100000}"
    return f"""<svg viewBox="0 0 {width} {height}" class="svg-chart" role="img" preserveAspectRatio="none">
  <defs>
    <linearGradient id="{grad_id}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{colour}" stop-opacity="0.35"/>
      <stop offset="100%" stop-color="{colour}" stop-opacity="0"/>
    </linearGradient>
  </defs>
  {''.join(grid)}
  <polygon points="{area_pts}" fill="url(#{grad_id})"/>
  <polyline points="{line_pts}" fill="none" stroke="{colour}" stroke-width="2.4"
    stroke-linejoin="round" stroke-linecap="round"/>
  {dots}
  <circle cx="{lx:.1f}" cy="{ly:.1f}" r="4" fill="{colour}" stroke="#0f1115" stroke-width="2"/>
  {''.join(label_axis)}
  <text x="{pad_l}" y="{height - 8}" text-anchor="start" class="svg-axis">{first_date}</text>
  <text x="{width - pad_r}" y="{height - 8}" text-anchor="end" class="svg-axis">{last_date}</text>
  <text x="{lx:.1f}" y="{ly - 9:.1f}" text-anchor="end" class="svg-value" fill="{colour}">{last_label}</text>
</svg>"""


def progress_ring(pct: float, *, label: str = "", sub: str = "", colour: str = ACCENT) -> str:
    """A circular gauge filled to ``pct`` (0-100)."""
    pct = max(0.0, min(100.0, float(pct)))
    size, stroke = 132, 12
    r = (size - stroke) / 2
    cx = cy = size / 2
    circ = 2 * 3.141592653589793 * r
    dash = circ * pct / 100
    return f"""<svg viewBox="0 0 {size} {size}" class="svg-ring" role="img">
  <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{TRACK}" stroke-width="{stroke}"/>
  <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{colour}" stroke-width="{stroke}"
    stroke-linecap="round" stroke-dasharray="{dash:.1f} {circ:.1f}"
    transform="rotate(-90 {cx} {cy})"/>
  <text x="{cx}" y="{cy - 2}" text-anchor="middle" class="svg-ring-value">{_esc(label)}</text>
  <text x="{cx}" y="{cy + 16}" text-anchor="middle" class="svg-ring-sub">{_esc(sub)}</text>
</svg>"""


def donut(segments: Sequence[dict], *, size: int = 160) -> str:
    """A donut chart. ``segments`` = ``[{"label", "value", "colour"?}]``."""
    items = [s for s in segments if float(s.get("value", 0)) > 0]
    total = sum(float(s["value"]) for s in items)
    if total <= 0:
        return _empty_chart("No data", size, size)

    stroke = 26
    r = (size - stroke) / 2
    cx = cy = size / 2
    circ = 2 * 3.141592653589793 * r
    offset = 0.0
    arcs = []
    legend = []
    for i, seg in enumerate(items):
        colour = seg.get("colour") or SERIES_COLOURS[i % len(SERIES_COLOURS)]
        frac = float(seg["value"]) / total
        dash = circ * frac
        arcs.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{colour}" '
            f'stroke-width="{stroke}" stroke-dasharray="{dash:.2f} {circ:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" transform="rotate(-90 {cx} {cy})"/>'
        )
        offset += dash
        legend.append(
            f'<div class="legend-row"><span class="legend-dot" style="background:{colour}"></span>'
            f'<span class="legend-label">{_esc(seg["label"])}</span>'
            f'<span class="legend-val">{_fmt(round(frac * 100))}%</span></div>'
        )
    svg = f"""<svg viewBox="0 0 {size} {size}" class="svg-donut" role="img">
  <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{TRACK}" stroke-width="{stroke}"/>
  {''.join(arcs)}
</svg>"""
    return f'<div class="donut-wrap">{svg}<div class="legend">{"".join(legend)}</div></div>'


def bar_chart(bars: Sequence[dict], *, colour: str = ACCENT, unit: str = "", height: int = 200) -> str:
    """Vertical bars. ``bars`` = ``[{"label", "value", "caption"?}]``."""
    items = list(bars)
    if not items:
        return _empty_chart("No data yet", 720, height)
    width = 720
    pad_b, pad_t = 26, 18
    plot_h = height - pad_b - pad_t
    hi = max(float(b["value"]) for b in items) or 1
    n = len(items)
    gap = 8
    bar_w = (width - gap * (n + 1)) / n
    rects = []
    labels = []
    for i, b in enumerate(items):
        v = float(b["value"])
        bh = (v / hi) * plot_h
        x = gap + i * (bar_w + gap)
        y = pad_t + (plot_h - bh)
        rects.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" '
            f'rx="4" fill="{colour}"><title>{_esc(b.get("caption") or b["label"])}: '
            f'{_nice_round(v)} {_esc(unit)}</title></rect>'
        )
        labels.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{height - 8}" text-anchor="middle" '
            f'class="svg-axis">{_esc(b["label"])}</text>'
        )
        if bh > 18:
            labels.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{y - 4:.1f}" text-anchor="middle" '
                f'class="svg-barval">{_nice_round(v)}</text>'
            )
    return f"""<svg viewBox="0 0 {width} {height}" class="svg-chart" role="img" preserveAspectRatio="none">
  {''.join(rects)}
  {''.join(labels)}
</svg>"""


def calendar_heatmap(levels: dict[str, int], *, weeks: int = 18, end: date | None = None) -> str:
    """A GitHub-style activity calendar.

    ``levels`` maps ISO date strings to an intensity 0-4.
    """
    end = end or date.today()
    # Start on the Monday of the earliest visible week.
    start = end - timedelta(days=weeks * 7 - 1)
    start -= timedelta(days=start.weekday())

    cell, gap = 15, 4
    cols = weeks + 1
    width = cols * (cell + gap) + 30
    height = 7 * (cell + gap) + 22

    palette = {
        0: TRACK,
        1: "#1f3d29",
        2: "#2e6b3f",
        3: "#3ba35a",
        4: ACCENT,
    }
    day_labels = ["Mon", "", "Wed", "", "Fri", "", "Sun"]
    squares = []
    for d in range(7):
        if day_labels[d]:
            squares.append(
                f'<text x="0" y="{22 + d * (cell + gap) + cell - 3}" class="svg-axis">{day_labels[d]}</text>'
            )
    current = start
    col = 0
    month_marks = []
    last_month = None
    while current <= end:
        col = (current - start).days // 7
        row = current.weekday()
        x = 30 + col * (cell + gap)
        y = 22 + row * (cell + gap)
        iso = current.isoformat()
        lvl = max(0, min(4, int(levels.get(iso, 0))))
        squares.append(
            f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="3" '
            f'fill="{palette[lvl]}"><title>{iso}</title></rect>'
        )
        if current.day <= 7 and current.month != last_month:
            month_marks.append(
                f'<text x="{x}" y="14" class="svg-axis">{current.strftime("%b")}</text>'
            )
            last_month = current.month
        current += timedelta(days=1)
    return f"""<svg viewBox="0 0 {width} {height}" class="svg-cal" role="img">
  {''.join(month_marks)}
  {''.join(squares)}
</svg>"""


def sparkline(values: Sequence[float], *, colour: str = ACCENT, width: int = 120, height: int = 34) -> str:
    """A tiny inline trend line."""
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    n = len(vals)
    pts = " ".join(
        f"{(i / (n - 1)) * (width - 4) + 2:.1f},"
        f"{(1 - (v - lo) / span) * (height - 6) + 3:.1f}"
        for i, v in enumerate(vals)
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" class="svg-spark" preserveAspectRatio="none">'
        f'<polyline points="{pts}" fill="none" stroke="{colour}" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round"/></svg>'
    )


def _empty_chart(message: str, width: int, height: int) -> str:
    return (
        f'<svg viewBox="0 0 {width} {height}" class="svg-chart svg-empty" role="img">'
        f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
        f'class="svg-axis">{_esc(message)}</text></svg>'
    )
