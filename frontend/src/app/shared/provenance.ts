/** Mirror of `backend/provenance.py::api_view` (contract version 1). */
export type ProvenanceKind = 'imported' | 'user_entered' | 'derived';

export interface ProvenanceContent {
  values?: Record<string, number>;
  field_sources?: Record<string, string>;
  corrected_fields?: string[];
}

export interface Provenance {
  contract_version: number;
  kind: ProvenanceKind;
  source: string;
  source_label: string;
  label: string;
  authoritative: boolean;
  corrected: boolean;
  applied: boolean;
  revision: number;
  supersedes_revision: number | null;
  external_id: string | null;
  source_timestamp: string | null;
  ingested_at: string;
  transformation_version: string;
  confidence: number | null;
  /** Qualitative level (high/medium/low) for records whose engine reports no probability. */
  confidence_label?: string | null;
  note: string | null;
  content?: ProvenanceContent;
}

export interface BodyReading {
  date: string;
  weight_kg: number | null;
  body_fat_pct: number | null;
  muscle_pct: number | null;
  resting_hr: number | null;
  hrv: number | null;
  provenance: Provenance | null;
}

/** Shown when a record predates provenance tracking: never assume it is verified. */
export const UNRECORDED_LABEL = 'Source not recorded';

export function provenanceKind(provenance: Provenance | null | undefined): string {
  return provenance?.kind ?? 'unrecorded';
}

export function provenanceLabel(provenance: Provenance | null | undefined): string {
  return provenance?.label ?? UNRECORDED_LABEL;
}

function stamp(value: string | null): string {
  return value ? value.replace('T', ' ').replace(/:\d{2}(\.\d+)?(\+00:00|Z)?$/, '') : '';
}

/** Hover/long-form detail: where it came from, when, and how it was transformed. */
export function provenanceDetail(provenance: Provenance | null | undefined): string {
  if (!provenance) {
    return 'This record was stored before provenance tracking, so its origin is unknown.';
  }
  const parts = [provenance.label];
  if (provenance.source_timestamp) parts.push(`source time ${stamp(provenance.source_timestamp)}`);
  parts.push(`stored ${stamp(provenance.ingested_at)}`);
  parts.push(`transformation ${provenance.transformation_version}`);
  if (provenance.confidence !== null) {
    parts.push(`confidence ${Math.round(provenance.confidence * 100)}%`);
  } else if (provenance.confidence_label) {
    parts.push(`confidence ${provenance.confidence_label}`);
  }
  if (!provenance.authoritative) parts.push('a recommendation, not a measurement');
  return parts.join(' · ');
}
