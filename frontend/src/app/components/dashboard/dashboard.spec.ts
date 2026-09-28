import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Dashboard } from './dashboard';

describe('Dashboard', () => {
  it('renders the source-volume multiplier without corrupted text', () => {
    TestBed.configureTestingModule({
      imports: [Dashboard],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });

    const fixture = TestBed.createComponent(Dashboard);
    fixture.detectChanges();

    TestBed.inject(HttpTestingController)
      .expectOne('/api/dashboard')
      .flush({
        setup_required: false,
        week: 1,
        cycle_weeks: 4,
        weekday: 'Monday',
        focus: 'Push',
        block: {
          number: 1,
          name: 'Foundation',
          focus: 'Build consistency',
          rep_emphasis: '8–12',
          target_rir: 3,
          volume_multiplier: 0.9,
        },
        cycle_ring: '',
        block_ring: '',
        quote: 'Keep going',
        dashboard_insight: null,
        rows: [],
        lifestyle: [],
        review_headline: 'Keep logging',
        review_lifts: null,
        review_recovery: 'No recovery trend yet',
        streak: 0,
        calendar: '',
      });
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent as string;
    expect(text).toContain('0.9× source volume');
    expect(text).not.toContain('Õ');
  });
});
