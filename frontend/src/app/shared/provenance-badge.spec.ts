import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Provenance, provenanceDetail } from './provenance';
import { ProvenanceBadge } from './provenance-badge';

const base: Provenance = {
  contract_version: 1,
  kind: 'imported',
  source: 'hevy',
  source_label: 'Hevy',
  label: 'Imported from Hevy',
  authoritative: true,
  corrected: false,
  applied: true,
  revision: 1,
  supersedes_revision: null,
  external_id: 'w1',
  source_timestamp: '2026-01-01T06:30:00+00:00',
  ingested_at: '2026-01-02T03:04:05+00:00',
  transformation_version: 'hevy-parser/1',
  confidence: null,
  note: null,
};

@Component({
  imports: [ProvenanceBadge],
  template: `<app-provenance-badge [provenance]="provenance" />`,
})
class Host {
  provenance: Provenance | null = null;
}

function render(provenance: Provenance | null): HTMLElement {
  const fixture = TestBed.createComponent(Host);
  fixture.componentInstance.provenance = provenance;
  fixture.detectChanges();
  return fixture.nativeElement.querySelector('.prov-badge') as HTMLElement;
}

describe('ProvenanceBadge', () => {
  beforeEach(() => TestBed.configureTestingModule({ imports: [Host] }));

  it('labels imported source data', () => {
    const badge = render(base);
    expect(badge.textContent?.trim()).toBe('Imported from Hevy');
    expect(badge.classList).toContain('prov-imported');
    expect(badge.dataset['provenance']).toBe('imported');
  });

  it('labels a user correction distinctly from the data it corrects', () => {
    const badge = render({
      ...base,
      kind: 'user_entered',
      source: 'manual',
      source_label: 'You',
      label: 'Corrected by you',
      corrected: true,
      revision: 2,
      supersedes_revision: 1,
    });
    expect(badge.textContent?.trim()).toBe('Corrected by you');
    expect(badge.classList).toContain('prov-user_entered');
    expect(badge.classList).not.toContain('prov-imported');
  });

  it('labels derived recommendations and says they are not measurements', () => {
    const badge = render({
      ...base,
      kind: 'derived',
      source: 'ai:gemini/test',
      label: 'AI recommendation',
      authoritative: false,
      confidence: 0.82,
    });
    expect(badge.textContent?.trim()).toBe('AI recommendation');
    expect(badge.classList).toContain('prov-derived');
    expect(badge.title).toContain('confidence 82%');
    expect(badge.title).toContain('a recommendation, not a measurement');
  });

  it('never presents a record without provenance as verified source data', () => {
    const badge = render(null);
    expect(badge.textContent?.trim()).toBe('Source not recorded');
    expect(badge.classList).toContain('prov-unrecorded');
    expect(badge.title).toContain('origin is unknown');
  });
});

describe('provenanceDetail', () => {
  it('reports source time, ingestion time and the transformation version', () => {
    const detail = provenanceDetail(base);
    expect(detail).toContain('Imported from Hevy');
    expect(detail).toContain('source time 2026-01-01 06:30');
    expect(detail).toContain('stored 2026-01-02 03:04');
    expect(detail).toContain('transformation hevy-parser/1');
    expect(detail).not.toContain('confidence');
    expect(detail).not.toContain('recommendation');
  });
});
