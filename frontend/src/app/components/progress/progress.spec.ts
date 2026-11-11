import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Progress } from './progress';

describe('Progress explanations', () => {
  it('ignores a late response after closing and reopening an explanation', () => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    const http = TestBed.inject(HttpTestingController);
    const progress = TestBed.runInInjectionContext(() => new Progress());
    progress.toggleXAI('Squat');
    const old = http.expectOne((request) => request.url.includes('/api/xai_reasoning/'));
    progress.toggleXAI('Squat');
    progress.toggleXAI('Squat');
    const fresh = http.expectOne((request) => request.url.includes('/api/xai_reasoning/'));
    fresh.flush({ reasoning: 'Current explanation' });
    old.flush({ reasoning: 'Obsolete explanation' });
    expect(progress.xaiReasoning()['Squat']).toBe('Current explanation');
    progress.toggleXAI('Squat');
    expect(progress.xaiReasoning()['Squat']).toBeUndefined();
    http.verify();
  });
});

const original = {
  date: '2026-01-01',
  weight_kg: 91,
  body_fat_pct: 15,
  muscle_pct: null,
  resting_hr: 58,
  hrv: null,
  provenance: {
    contract_version: 1,
    kind: 'imported',
    source: 'google_health',
    source_label: 'Google Health',
    label: 'Imported from Google Health',
    authoritative: true,
    corrected: false,
    applied: true,
    revision: 1,
    supersedes_revision: null,
    external_id: null,
    source_timestamp: '2026-01-01T06:30:00+00:00',
    ingested_at: '2026-01-02T03:04:05+00:00',
    transformation_version: 'google-health-body/1',
    confidence: null,
    note: null,
  },
};

async function setup() {
  await TestBed.configureTestingModule({
    imports: [Progress],
    providers: [provideHttpClient(), provideHttpClientTesting()],
  }).compileComponents();
  const http = TestBed.inject(HttpTestingController);
  const fixture = TestBed.createComponent(Progress);
  fixture.detectChanges();
  http.expectOne('/api/progress').flush({ charts: [], body: {}, body_readings: [original] });
  await fixture.whenStable();
  fixture.detectChanges();
  return { http, fixture, el: fixture.nativeElement as HTMLElement };
}

describe('Progress body readings', () => {
  it('shows where each reading came from', async () => {
    const { el, http } = await setup();
    expect(el.querySelector('.reading-date')?.textContent).toBe('2026-01-01');
    expect(el.querySelector('.reading-values')?.textContent).toContain('91 kg');
    expect(el.querySelector('.prov-badge')?.textContent?.trim()).toBe(
      'Imported from Google Health',
    );
    http.verify();
  });

  it('submits only the changed fields and reloads, keeping the original in its history', async () => {
    const { http, fixture, el } = await setup();
    const progress = fixture.componentInstance;
    progress.startCorrection(original as any);
    progress.draft.values.weight_kg = '81';
    progress.draft.reason = ' Scale was on carpet ';
    progress.submitCorrection(original as any);

    const post = http.expectOne('/api/body-metrics/corrections');
    expect(post.request.method).toBe('POST');
    expect(post.request.body).toEqual({
      date: '2026-01-01',
      weight_kg: 81,
      reason: 'Scale was on carpet',
    });
    expect(Object.keys(post.request.body)).not.toContain('user_id');
    post.flush({ lineage: [{ ...original.provenance, revision: 2, kind: 'user_entered' }] });

    const reload = http.expectOne('/api/progress');
    reload.flush({
      charts: [],
      body: {},
      body_readings: [
        {
          ...original,
          weight_kg: 81,
          provenance: {
            ...original.provenance,
            kind: 'user_entered',
            label: 'Corrected by you',
            corrected: true,
            revision: 2,
          },
        },
      ],
    });
    await fixture.whenStable();
    fixture.detectChanges();
    expect(progress.editingDate()).toBeNull();
    expect(progress.correctionNotice()).toContain('original reading is kept');
    expect(el.querySelector('.prov-badge')?.textContent?.trim()).toBe('Corrected by you');
    expect(progress.lineage()['2026-01-01']).toHaveLength(1);
    http.verify();
  });

  it('asks for a change instead of sending an empty correction', async () => {
    const { http, fixture } = await setup();
    const progress = fixture.componentInstance;
    progress.startCorrection(original as any);
    progress.submitCorrection(original as any);
    expect(progress.correctionError()).toContain('Change at least one value');
    http.expectNone('/api/body-metrics/corrections');
    progress.draft.values.weight_kg = 'abc';
    progress.submitCorrection(original as any);
    expect(progress.correctionError()).toContain('must be a number');
    http.expectNone('/api/body-metrics/corrections');
    http.verify();
  });

  it('shows the server validation message and keeps the form open', async () => {
    const { http, fixture, el } = await setup();
    const progress = fixture.componentInstance;
    progress.startCorrection(original as any);
    progress.draft.values.weight_kg = '5';
    progress.submitCorrection(original as any);
    http
      .expectOne('/api/body-metrics/corrections')
      .flush(
        { detail: 'weight_kg must be between 20 and 400' },
        { status: 422, statusText: 'Unprocessable Entity' },
      );
    fixture.detectChanges();
    expect(progress.saving()).toBe(false);
    expect(progress.editingDate()).toBe('2026-01-01');
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('between 20 and 400');
    http.verify();
  });

  it('loads and hides the revision history of a reading', async () => {
    const { http, fixture, el } = await setup();
    const progress = fixture.componentInstance;
    progress.toggleLineage('2026-01-01');
    http.expectOne('/api/provenance/body_metrics/2026-01-01').flush({
      revisions: [
        {
          ...original.provenance,
          kind: 'user_entered',
          label: 'Corrected by you',
          revision: 3,
          applied: false,
          note: 'Not applied: a user-entered value takes precedence over this import.',
          content: { values: { weight_kg: 91 } },
        },
        {
          ...original.provenance,
          content: { values: { weight_kg: 91, body_fat_pct: 15 } },
        },
      ],
    });
    fixture.detectChanges();
    const rows = el.querySelectorAll('.lineage-list li');
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain('not applied');
    expect(rows[1].textContent).toContain('91 kg · 15 % body fat');
    progress.toggleLineage('2026-01-01');
    fixture.detectChanges();
    expect(el.querySelector('.lineage-list')).toBeNull();
    http.verify();
  });

  it('labels an AI explanation as a derived recommendation', async () => {
    const { http, fixture } = await setup();
    const progress = fixture.componentInstance;
    progress.toggleXAI('Squat');
    http
      .expectOne((request) => request.url.includes('/api/xai_reasoning/'))
      .flush({
        reasoning: 'Volume dropped.',
        provenance: { ...original.provenance, kind: 'derived', label: 'AI recommendation' },
      });
    expect(progress.xaiProvenance()['Squat']?.kind).toBe('derived');
    http.verify();
  });
});
