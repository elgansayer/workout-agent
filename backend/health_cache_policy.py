"""HTTP cache policy for personalized health endpoints."""

from __future__ import annotations


PERSONALIZED_HEALTH_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "Vary": "Cookie, Authorization",
}
