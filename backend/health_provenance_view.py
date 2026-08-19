"""Safe provenance projection for explaining health metric sources."""

from __future__ import annotations

from dataclasses import dataclass

from health_models import SourceProvenance


@dataclass(frozen=True, slots=True)
class ProvenanceView:
    provider: str
    source_device: str | None
    source_app: str | None
    data_origin: str | None
    normalization_version: int


def provenance_view(source: SourceProvenance) -> ProvenanceView:
    return ProvenanceView(source.provider, source.source_device, source.source_app, source.data_origin, source.normalization_version)
