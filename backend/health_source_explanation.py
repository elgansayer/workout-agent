"""User-facing explanation for source precedence decisions."""

from __future__ import annotations

from health_models import HealthMetric


def explain_source(selected: HealthMetric, candidates: list[HealthMetric]) -> str:
    providers = sorted({item.provenance.provider for item in candidates})
    if len(providers) <= 1:
        return f"Using {selected.provenance.provider} because it is the available source."
    return f"Using {selected.provenance.provider} according to the configured source precedence; available sources: {', '.join(providers)}."
