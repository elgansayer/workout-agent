"""Tests for the server-rendered SVG chart helpers."""

from __future__ import annotations

from datetime import date

from webapp import charts


def test_line_chart_renders_svg_with_points():
    points = [
        {"date": "01-01", "value": 100.0, "label": "100 kg"},
        {"date": "01-08", "value": 105.0, "label": "105 kg"},
        {"date": "01-15", "value": 110.0, "label": "110 kg"},
    ]
    svg = charts.line_chart(points, unit="kg")
    assert svg.startswith("<svg")
    assert "polyline" in svg
    assert "110 kg" in svg


def test_line_chart_handles_too_little_data():
    svg = charts.line_chart([{"date": "01-01", "value": 100.0}])
    assert "Not enough data" in svg


def test_line_chart_escapes_labels():
    points = [
        {"date": "01-01", "value": 1, "label": "<script>"},
        {"date": "01-02", "value": 2, "label": "ok"},
    ]
    svg = charts.line_chart(points)
    assert "<script>" not in svg


def test_progress_ring_clamps_and_renders():
    svg = charts.progress_ring(150, label="Wk 6", sub="of 12")
    assert "Wk 6" in svg
    assert "<circle" in svg


def test_donut_renders_segments_and_legend():
    out = charts.donut(
        [
            {"label": "Legs", "value": 3},
            {"label": "Back", "value": 2},
        ]
    )
    assert "Legs" in out
    assert "legend" in out


def test_donut_empty_when_no_values():
    out = charts.donut([{"label": "x", "value": 0}])
    assert "No data" in out


def test_bar_chart_renders():
    svg = charts.bar_chart([{"label": "a", "value": 10}, {"label": "b", "value": 20}])
    assert "<rect" in svg


def test_calendar_heatmap_marks_levels():
    today = date(2026, 6, 17)
    svg = charts.calendar_heatmap({"2026-06-16": 4}, end=today)
    assert svg.startswith("<svg")
    assert "2026-06-16" in svg


def test_sparkline_needs_two_points():
    assert charts.sparkline([1.0]) == ""
    assert charts.sparkline([1.0, 2.0, 3.0]).startswith("<svg")
