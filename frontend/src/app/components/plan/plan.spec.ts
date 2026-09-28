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
});
