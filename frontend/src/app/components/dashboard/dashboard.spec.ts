import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Dashboard } from './dashboard';

describe('Dashboard refresh', () => {
  it('reloads the next workout when returning to an already-open app', () => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    const http = TestBed.inject(HttpTestingController);
    const dashboard = TestBed.runInInjectionContext(() => new Dashboard());

    dashboard.ngOnInit();
    http.expectOne('/api/dashboard').flush({ focus: 'Upper A' });
    expect(dashboard.data().focus).toBe('Upper A');

    dashboard.onWindowFocus();
    http.expectOne('/api/dashboard').flush({ focus: 'Upper B' });
    expect(dashboard.data().focus).toBe('Upper B');
    expect(dashboard.loading()).toBe(false);
    http.verify();
  });
});

describe('Compact dashboard', () => {
  it('starts with collapsed details and keeps large reviews searchable and paginated', async () => {
    await TestBed.configureTestingModule({
      imports: [Dashboard],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    const http = TestBed.inject(HttpTestingController);
    const fixture = TestBed.createComponent(Dashboard);
    fixture.detectChanges();
    http.expectOne('/api/dashboard').flush({
      week: 2,
      cycle_weeks: 12,
      block: {},
      focus: 'Upper body',
      rows: Array.from({ length: 11 }, (_, i) => ({ name: `Exercise ${i}`, planned: '3 × 10' })),
      review_lifts: Array.from({ length: 50 }, (_, i) => ({
        name: `Lift ${i}`,
        trend: i % 2 ? 'stalling' : 'progressing',
      })),
    });
    await fixture.whenStable();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelectorAll('.exercise')).toHaveLength(11);
    expect(el.querySelectorAll('.review-row')).toHaveLength(6);
    expect(el.querySelector('details[open]')).toBeNull();
    fixture.componentInstance.reviews.search('Lift 49');
    await fixture.whenStable();
    expect(el.querySelectorAll('.review-row')).toHaveLength(1);
    expect(el.querySelector('.review-name')?.textContent).toBe('Lift 49');
    fixture.componentInstance.filterReviews('progressing');
    await fixture.whenStable();
    expect(el.querySelectorAll('.review-row')).toHaveLength(0);
    expect(el.textContent).toContain('No matches');
    http.verify();
  });

  it('retains the last workout and provides retry when refreshing fails', () => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    const http = TestBed.inject(HttpTestingController);
    const dashboard = TestBed.runInInjectionContext(() => new Dashboard());
    dashboard.ngOnInit();
    http.expectOne('/api/dashboard').flush({ focus: 'Upper body' });
    dashboard.refresh();
    dashboard.refresh();
    http.expectOne('/api/dashboard').flush({}, { status: 503, statusText: 'Unavailable' });
    expect(dashboard.data().focus).toBe('Upper body');
    expect(dashboard.loading()).toBe(false);
    expect(dashboard.error()).toBeTruthy();
    http.verify();
  });
});
