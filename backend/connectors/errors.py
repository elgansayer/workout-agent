"""Common provider error mapping for connector implementations."""

from __future__ import annotations

from .base import ConnectorError


def map_http_error(status_code: int, *, provider: str) -> ConnectorError:
    if status_code == 401:
        return ConnectorError(f"{provider} authorization expired", code="unauthorized", retryable=False)
    if status_code == 403:
        return ConnectorError(f"{provider} permission denied", code="forbidden", retryable=False)
    if status_code == 429:
        return ConnectorError(f"{provider} rate limit reached", code="rate_limited", retryable=True)
    if 500 <= status_code <= 599:
        return ConnectorError(f"{provider} is temporarily unavailable", code="provider_unavailable", retryable=True)
    return ConnectorError(f"{provider} request failed", code="provider_error", retryable=False)
