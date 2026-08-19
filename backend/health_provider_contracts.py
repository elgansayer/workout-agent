"""Explicit provider contract identifiers used by fixtures and sync runs."""

from __future__ import annotations


PROVIDER_CONTRACTS = {
    "oura": "api-v2",
    "polar": "accesslink-dynamic-v4",
    "fitbit": "web-api-current-validation-required",
    "garmin": "developer-program-approved-contract-required",
    "withings": "public-api-current",
    "health_connect": "android-sdk-companion",
}


def contract_version(provider: str) -> str:
    try:
        return PROVIDER_CONTRACTS[provider]
    except KeyError as exc:
        raise KeyError(f"unknown health provider contract: {provider}") from exc
