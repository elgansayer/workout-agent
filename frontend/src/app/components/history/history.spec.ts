import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { History } from './history';

const derived = {
  contract_version: 1,
  kind: 'derived',
  source: 'ai:gemini/test',
  source_label: 'AI coach (Gemini test)',
  label: 'AI recommendation',
  authoritative: false,
  corrected: false,
  applied: true,
  revision: 1,
  supersedes_revision: null,
  external_id: null,
  source_timestamp: null,
  ingested_at: '2026-01-02T03:04:05+00:00',
  transformation_version: 'daily-plan/1',
  confidence: null,
  note: null,
};

describe('History provenance', () => {
  it('marks generated daily plans as recommendations and legacy logs as unrecorded', async () => {
    await TestBed.configureTestingModule({
      imports: [History],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    const http = TestBed.inject(HttpTestingController);
    const fixture = TestBed.createComponent(History);
    fixture.detectChanges();
    http.expectOne('/api/history').flush({
      calendar: '',
      logs: [
        {
          date: '2026-01-02',
          day: 1,
          focus: 'Legs',
          carb_tier: 'high',
          plan: 'Squat',
          provenance: derived,
        },
        {
          date: '2026-01-01',
          day: 2,
          focus: 'Pull',
          carb_tier: 'low',
          plan: 'Row',
          provenance: null,
        },
      ],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    const badges = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('.log-head .prov-badge'),
    );
    expect(badges.map((badge) => badge.textContent?.trim())).toEqual([
      'AI recommendation',
      'Source not recorded',
    ]);
    expect(badges[0].classList).toContain('prov-derived');
    expect(badges[1].classList).toContain('prov-unrecorded');
    http.verify();
  });
});
