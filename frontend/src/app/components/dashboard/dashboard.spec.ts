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

describe('Dashboard shared readiness', () => {
  const component = (connector: string, label: string, state: string, message: string) => ({
    connector,
    label,
    state,
    reason: 'synthetic',
    message,
    next_action: 'sync',
    action_label: state === 'ready' ? null : 'Sync now',
    action_href: state === 'ready' ? null : '/settings?section=connections',
  });

  async function render(payload: object) {
    await TestBed.configureTestingModule({
      imports: [Dashboard],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    const fixture = TestBed.createComponent(Dashboard);
    fixture.detectChanges();
    TestBed.inject(HttpTestingController).expectOne('/api/dashboard').flush(payload);
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  const base = { week: 2, cycle_weeks: 12, block: {}, focus: 'Upper', rows: [] };

  it('explains stale Hevy data with the shared message and action', async () => {
    const el = await render({
      ...base,
      readiness: {
        components: {
          hevy: component('hevy', 'Hevy', 'stale', 'Hevy was last synced 5 days ago.'),
          programme: component('programme', 'Programme', 'ready', 'Your programme is active.'),
        },
      },
    });
    const notice = el.querySelector('.status-notice[data-connector="hevy"]');
    expect(notice?.textContent).toContain('Hevy was last synced 5 days ago.');
    expect(notice?.querySelector('a')?.getAttribute('href')).toBe('/settings?section=connections');
    expect(el.querySelector('.status-notice[data-connector="programme"]')).toBeNull();
  });

  it('adds no notice noise when everything is ready', async () => {
    const el = await render({
      ...base,
      readiness: {
        components: {
          hevy: component('hevy', 'Hevy', 'ready', 'Hevy is up to date.'),
          programme: component('programme', 'Programme', 'ready', 'Your programme is active.'),
        },
      },
    });
    expect(el.querySelector('.status-notice')).toBeNull();
  });

  it('uses the programme call to action once, driven by the server-derived flag', async () => {
    const el = await render({
      ...base,
      setup_required: true,
      readiness: {
        components: {
          hevy: component('hevy', 'Hevy', 'setup_required', 'Hevy is not set up yet.'),
          programme: component(
            'programme',
            'Programme',
            'setup_required',
            'Build your programme from Hevy to see your plan.',
          ),
        },
      },
    });
    expect(el.textContent).toContain('Build your programme from Hevy');
    expect(el.querySelector('.status-notice[data-connector="hevy"]')).not.toBeNull();
    expect(el.querySelector('.status-notice[data-connector="programme"]')).toBeNull();
  });

  it('flags a programme that needs review', async () => {
    const el = await render({
      ...base,
      readiness: {
        components: {
          programme: component(
            'programme',
            'Programme',
            'degraded',
            'Your programme needs review before it can be used.',
          ),
        },
      },
    });
    const notice = el.querySelector('.status-notice[data-connector="programme"]');
    expect(notice?.getAttribute('role')).toBe('alert');
  });
});
