"""Framework-neutral health integration API contract definitions."""

from __future__ import annotations


HEALTH_API_ROUTES = {
    "list_connections": ("GET", "/api/health/connections"),
    "provider_status": ("GET", "/api/health/providers/{provider}/status"),
    "test_provider": ("POST", "/api/health/providers/{provider}/test"),
    "backfill": ("POST", "/api/health/connections/{connection_id}/backfill"),
    "disconnect": ("DELETE", "/api/health/connections/{connection_id}"),
    "health_connect_upload": ("POST", "/api/health/health-connect/upload"),
    "daily_readiness": ("GET", "/api/health/readiness/today"),
}

PUBLIC_HEALTH_API_ROUTES = frozenset()
