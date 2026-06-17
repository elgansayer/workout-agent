"""Reads recovery metrics exported from Google Health Connect.

Health Connect data is locked to the Android device for privacy. The intended
flow is to use an app such as Health Sync or Tasker to drop a small daily JSON
file into a folder synced to this machine. This module reads that file if it
exists. If it is missing or malformed, the agent simply runs without recovery
data.

Expected JSON shape:
    {"date": "2026-06-17", "sleep_hours": 7.5, "weight_kg": 82.0, "resting_hr": 58}
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def read_recovery_metrics(file_path: str | None) -> dict[str, Any] | None:
    """Return recovery metrics from the exported file, or None if unavailable."""
    if not file_path:
        return None

    path = Path(file_path)
    if not path.is_file():
        logger.info("Health Connect file not found at %s; skipping.", file_path)
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read Health Connect file: %s", exc)
        return None

    if not isinstance(data, dict):
        logger.warning("Health Connect file did not contain a JSON object.")
        return None

    return data
