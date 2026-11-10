import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Plan } from './plan';

describe('Plan', () => {
  it('renders programme multipliers and confidence separators without corrupted text', () => {
    TestBed.configureTestingModule({
      imports: [Plan],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });

    const fixture = TestBed.createComponent(Plan);
    fixture.detectChanges();

    TestBed.inject(HttpTestingController)
      .expectOne('/api/plan')
      .flush({
        setup_required: false,
        split_name: 'Imported rotation',
        week: 1,
        cycle_weeks: 4,
        current_block: 'Foundation',
        analysis: { confidence: 'medium' },
        warnings: [],
        blocks: [
          {
            number: 1,
            name: 'Foundation',
            is_current: true,
            weeks: '1–4',
            start_date: '2026-09-28',
            end_date: '2026-10-25',
            focus: 'Build consistency',
            volume_multiplier: 0.9,
            target_rir: 3,
            rep_emphasis: '8–12',
            specificity: 'General',
          },
        ],
        days: [],
        rules: [],
      });
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent as string;
    expect(text).toContain('· inference confidence medium');
    expect(text).toContain('0.9× imported source');
    expect(text).not.toContain('Õ');
    expect(text).not.toContain("¡'");
  });

  it('labels the active programme as a rules-derived recommendation', () => {
    TestBed.configureTestingModule({
      imports: [Plan],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    const fixture = TestBed.createComponent(Plan);
    fixture.detectChanges();

    TestBed.inject(HttpTestingController)
      .expectOne('/api/plan')
      .flush({
        setup_required: false,
        split_name: 'Imported rotation',
        week: 1,
        cycle_weeks: 4,
        analysis: { confidence: 'medium' },
        provenance: {
          kind: 'derived',
          label: 'Calculated by rules',
          authoritative: false,
          confidence: null,
          confidence_label: 'medium',
          source_timestamp: '2026-08-20T10:00:00+00:00',
          ingested_at: '2026-09-01T10:00:00+00:00',
          transformation_version: 'hevy-programme-v1',
        },
        warnings: [],
        blocks: [],
        days: [],
        rules: [],
      });
    fixture.detectChanges();

    const badge = (fixture.nativeElement as HTMLElement).querySelector('header .prov-badge');
    expect(badge?.textContent?.trim()).toBe('Calculated by rules');
    expect(badge?.classList).toContain('prov-derived');
    expect((badge as HTMLElement).title).toContain('transformation hevy-programme-v1');
    expect((badge as HTMLElement).title).toContain('confidence medium');
  });

  it('shows an explicit state when the programme is completed', async () => {
    await TestBed.configureTestingModule({
      imports: [Plan],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();

    const fixture = TestBed.createComponent(Plan);
    fixture.detectChanges();
    TestBed.inject(HttpTestingController)
      .expectOne('/api/plan')
      .flush({
        setup_required: false,
        programme_status: 'completed',
        rotation_message:
          'This programme is complete. Review it before starting a new programme.',
        split_name: 'Imported rotation',
        week: 8,
        cycle_weeks: 8,
        blocks: [],
        days: [],
      });
    await fixture.whenStable();
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent as string;
    expect(text).toContain('Programme Completed');
    expect(text).toContain('This programme is complete.');
    expect(text).toContain('Review programme');
  });
});
