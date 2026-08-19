"""Shared bounds for provider payload ingestion."""

from __future__ import annotations

import json
from typing import Any


MAX_PAYLOAD_BYTES = 2 * 1024 * 1024


def parse_bounded_json(payload: bytes) -> Any:
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("health provider payload exceeds size limit")
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid health provider JSON") from exc
