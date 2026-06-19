from __future__ import annotations
import math
from typing import Sequence
import analytics
from webapp import charts

# Colors
ACCENT = "#4ade80"
ACCENT_2 = "#38bdf8"
WARN = "#f59e0b"
PINK = "#f472b6"
GRID = "#262c37"
TRACK = "#1e232c"
MUTED = "#9aa3b2"

def block_phase_tracker(session_volumes: list[dict]) -> str:
    """Widget 1: Automated Block Phase Tracker Widget
    Renders a line chart highlighting volume accumulation vs intensification.
    """
    if len(session_volumes) < 2:
        return "<div class='muted'>Not enough data to track block phases.</div>"
    
    width, height = 720, 220
    pad_l, pad_r, pad_t, pad_b = 48, 16, 18, 28
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    
    volumes = [v["volume"] for v in session_volumes[-30:]] # last 30 sessions
    dates = [v["date"][5:] for v in session_volumes[-30:]]
    if not volumes:
        return ""
        
    lo, hi = min(volumes), max(volumes)
    if hi == lo:
        hi += 1
        lo -= 1
    span = hi - lo
    
    def x(i: int) -> float:
        return pad_l + (i / (len(volumes) - 1)) * plot_w
    def y(v: float) -> float:
        return pad_t + (1 - (v - lo) / span) * plot_h
        
    coords = [(x(i), y(v)) for i, v in enumerate(volumes)]
    line_pts = " ".join(f"{cx:.1f},{cy:.1f}" for cx, cy in coords)
    
    # Highlight zones
    # We can calculate moving average to determine trend
    svg = f"""<svg viewBox="0 0 {width} {height}" class="svg-chart" role="img" preserveAspectRatio="none">
        <text x="10" y="20" fill="{ACCENT_2}" font-size="12" font-weight="bold">Phase Tracker: Volume vs Intensity</text>
        <polyline points="{line_pts}" fill="none" stroke="{ACCENT_2}" stroke-width="2.5" stroke-linejoin="round"/>
    """
    for cx, cy in coords:
        svg += f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3" fill="{ACCENT_2}"/>'
    
    svg += f'<text x="{pad_l}" y="{height - 8}" fill="{MUTED}" font-size="11">{dates[0]}</text>'
    svg += f'<text x="{width - pad_r}" y="{height - 8}" text-anchor="end" fill="{MUTED}" font-size="11">{dates[-1]}</text>'
    svg += "</svg>"
    
    return f"<div class='chart-card ai-glass'>{svg}</div>"

def systemic_recovery_correlation(biometrics: list[dict], session_volumes: list[dict]) -> str:
    """Widget 2: Systemic Recovery Correlation Grid
    Scatter plot: X = Sleep/RHR, Y = Volume. Highlights anomalies.
    """
    width, height = 720, 240
    pad_l, pad_r, pad_t, pad_b = 48, 16, 20, 30
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    
    # We need to map biometrics (resting_hr) to volume on the same date
    vol_by_date = {v["date"]: v["volume"] for v in session_volumes}
    
    data_points = []
    for b in biometrics:
        d = b["date"]
        rhr = b.get("resting_hr")
        if rhr and d in vol_by_date:
            data_points.append({"rhr": rhr, "vol": vol_by_date[d], "date": d})
            
    if len(data_points) < 3:
        return "<div class='muted'>Not enough overlapping data for correlation grid.</div>"
        
    xs = [dp["rhr"] for dp in data_points]
    ys = [dp["vol"] for dp in data_points]
    
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    
    if max_x == min_x: max_x += 1; min_x -= 1
    if max_y == min_y: max_y += 1; min_y -= 1
    
    span_x = max_x - min_x
    span_y = max_y - min_y
    
    circles = []
    for dp in data_points:
        cx = pad_l + ((dp["rhr"] - min_x) / span_x) * plot_w
        cy = pad_t + (1 - (dp["vol"] - min_y) / span_y) * plot_h
        
        # Highlight anomalies (high RHR, low volume)
        color = PINK if dp["rhr"] > (min_x + span_x * 0.7) and dp["vol"] < (min_y + span_y * 0.3) else ACCENT
        radius = 5 if color == PINK else 3
        opacity = 0.9 if color == PINK else 0.6
        
        circles.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius}" fill="{color}" opacity="{opacity}"><title>{dp["date"]}: RHR {dp["rhr"]}, Vol {dp["vol"]:.0f}</title></circle>')
        
    svg = f"""<svg viewBox="0 0 {width} {height}" class="svg-chart" role="img" preserveAspectRatio="none">
        <text x="{width/2}" y="15" fill="{MUTED}" font-size="12" text-anchor="middle">Systemic Recovery Correlation (RHR vs Volume)</text>
        <line x1="{pad_l}" y1="{height-pad_b}" x2="{width-pad_r}" y2="{height-pad_b}" stroke="{GRID}" stroke-width="2"/>
        <line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{height-pad_b}" stroke="{GRID}" stroke-width="2"/>
        <text x="{width/2}" y="{height - 5}" fill="{MUTED}" font-size="11" text-anchor="middle">Resting Heart Rate (bpm)</text>
        <text x="15" y="{height/2}" fill="{MUTED}" font-size="11" text-anchor="middle" transform="rotate(-90 15 {height/2})">Training Volume</text>
        <text x="{pad_l}" y="{height - pad_b + 12}" fill="{MUTED}" font-size="10" text-anchor="middle">{min_x}</text>
        <text x="{width - pad_r}" y="{height - pad_b + 12}" fill="{MUTED}" font-size="10" text-anchor="middle">{max_x}</text>
        {''.join(circles)}
    </svg>"""
    
    return f"<div class='chart-card ai-glass'>{svg}</div>"

def volume_distribution(groups: dict) -> str:
    """Widget 3: Automated Volume Distribution Chart
    Bar chart comparing actual vs ideal balance.
    """
    if not groups:
        return ""
        
    width, height = 720, 240
    pad_l, pad_r, pad_t, pad_b = 48, 16, 20, 30
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    
    # Ideal split balance (approximate)
    ideal = {"Chest": 0.20, "Back": 0.25, "Legs": 0.30, "Shoulders": 0.15, "Arms": 0.10}
    
    total_vol = sum(groups.values())
    if total_vol == 0: return ""
    
    labels = list(groups.keys())
    actual_pcts = {k: v / total_vol for k, v in groups.items()}
    
    bar_w = (plot_w / len(labels)) * 0.4
    gap = (plot_w / len(labels))
    
    max_pct = max(max(actual_pcts.values()), max(ideal.values())) * 1.2
    
    rects = []
    texts = []
    
    for i, label in enumerate(labels):
        x_center = pad_l + (i + 0.5) * gap
        
        # Actual Bar
        act_h = (actual_pcts[label] / max_pct) * plot_h
        act_y = pad_t + plot_h - act_h
        rects.append(f'<rect x="{x_center - bar_w - 2}" y="{act_y}" width="{bar_w}" height="{act_h}" fill="{ACCENT}" rx="2"><title>Actual {label}: {actual_pcts[label]*100:.1f}%</title></rect>')
        
        # Ideal Bar
        id_val = ideal.get(label, 0)
        id_h = (id_val / max_pct) * plot_h
        id_y = pad_t + plot_h - id_h
        rects.append(f'<rect x="{x_center + 2}" y="{id_y}" width="{bar_w}" height="{id_h}" fill="{MUTED}" opacity="0.5" rx="2"><title>Ideal {label}: {id_val*100:.1f}%</title></rect>')
        
        texts.append(f'<text x="{x_center}" y="{height - 10}" fill="{MUTED}" font-size="11" text-anchor="middle">{label}</text>')
        
    svg = f"""<svg viewBox="0 0 {width} {height}" class="svg-chart" role="img" preserveAspectRatio="none">
        <text x="{width/2}" y="15" fill="{MUTED}" font-size="12" text-anchor="middle">Volume Distribution: Actual (Green) vs Ideal (Grey)</text>
        <line x1="{pad_l}" y1="{height-pad_b}" x2="{width-pad_r}" y2="{height-pad_b}" stroke="{GRID}" stroke-width="2"/>
        {''.join(rects)}
        {''.join(texts)}
    </svg>"""
    
    return f"<div class='chart-card ai-glass'>{svg}</div>"
