import { Type } from '@angular/core';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { SetupState, SetupStateKind } from '../shared/setup-state';
import { Checkins } from './checkins/checkins';
import { Dashboard } from './dashboard/dashboard';
import { History } from './history/history';
import { Plan } from './plan/plan';
import { Progress } from './progress/progress';
import { Stats } from './stats/stats';

/**
 * Every data surface renders the same readiness contract: a first-run welcome,
 * a polite "syncing" status, an alert when the connection is degraded, and a
 * retryable error when the request itself fails.  Data that exists is still
 * shown beside a notice, and a surface never shows its content as if all were
 * well while something is not.
 */

function setup(state: SetupStateKind, overrides: Partial<SetupState> = {}): SetupState {
  return {
    state,
    label: `label for ${state}`,
    reason: '',
    action: null,
    missing: [],
    last_activity_at: null,
    age_days: null,
    stale: false,
    ...overrides,
  };
}

const CONNECT = {
  id: 'connect_hevy',
  label: 'Connect Hevy',
  href: '/settings',
  description: 'Add your Hevy API key in Settings.',
};
const RETRY = {
  id: 'retry_sync',
  label: 'Retry Hevy sync',
  href: '/settings',
  description: 'Run the history sync again from Settings.',
};

interface Surface {
  name: string;
  component: Type<unknown>;
  url: string;
  /** A payload with real content, without a setup block. */
  withData: Record<string, unknown>;
  /** A payload with no content, without a setup block. */
  withoutData: Record<string, unknown>;
  /** Text only the populated view renders. */
  content: string;
}

const SURFACES: Surface[] = [
  {
    name: 'Home',
    component: Dashboard,
    url: '/api/dashboard',
    withData: {
      week: 2,
      cycle_weeks: 12,
      block: { number: 1, name: 'Foundation' },
      focus: 'Upper body',
      rows: [{ name: 'Bench Press', planned: '3 × 8' }],
    },
    withoutData: { setup_required: true, rows: [], block: {} },
    content: 'Upper body',
  },
  {
    name: 'Progress',
    component: Progress,
    url: '/api/progress',
    withData: {
      charts: [{ name: 'Bench Press', svg: '' }],
      exercises: ['Bench Press'],
      series: { 'Bench Press': [{ date: '2026-09-01', top_weight_kg: 80, top_reps: 8 }] },
      body_readings: [{ date: '2026-09-01', weight_kg: 82, provenance: null }],
    },
    withoutData: { charts: [], exercises: [], series: {}, body_readings: [] },
    content: 'Body readings',
  },
  {
    name: 'Stats',
    component: Stats,
    url: '/api/stats',
    withData: {
      total_sessions: 12,
      exercises_tracked: 4,
      total_volume: 4000,
      weight_unit: 'kg',
      personal_records: [],
      projections: [],
    },
    withoutData: { total_sessions: 0, exercises_tracked: 0, personal_records: [] },
    content: 'Sessions logged',
  },
  {
    name: 'History',
    component: History,
    url: '/api/history',
    withData: {
      calendar: '',
      logs: [{ date: '2026-09-01', day: 1, focus: 'Legs', carb_tier: 'high', plan: 'Squat' }],
    },
    withoutData: { calendar: '', logs: [] },
    content: 'Training calendar',
  },
  {
    name: 'Check-ins',
    component: Checkins,
    url: '/api/checkins',
    withData: {
      checkins: [
        { number: 1, date: '2026-09-01', workouts_done: 12, weeks: 4, message: 'Solid block.' },
      ],
    },
    withoutData: { checkins: [] },
    content: 'Check-in 1',
  },
  {
    name: 'Plan',
    component: Plan,
    url: '/api/plan',
    withData: {
      setup_required: false,
      programme_name: 'Synthetic',
      routines: [],
      blocks: [],
    },
    withoutData: { setup_required: true },
    content: 'Active Hevy programme',
  },
];

describe('Surface readiness', () => {
  async function render(
    surface: Surface,
    payload: Record<string, unknown>,
  ): Promise<{ fixture: ComponentFixture<unknown>; el: HTMLElement; http: HttpTestingController }> {
    await TestBed.configureTestingModule({
      imports: [surface.component],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    const http = TestBed.inject(HttpTestingController);
    const fixture = TestBed.createComponent(surface.component);
    fixture.detectChanges();
    http.expectOne(surface.url).flush(payload);
    await fixture.whenStable();
    fixture.detectChanges();
    return { fixture, el: fixture.nativeElement as HTMLElement, http };
  }

  for (const surface of SURFACES) {
    describe(surface.name, () => {
      it('welcomes a brand-new account with the first step instead of an empty page', async () => {
        const { el } = await render(surface, {
          ...surface.withoutData,
          setup: setup('setup_required', {
            first_run: true,
            reason: 'Hevy is not connected, so no routines or workouts can be imported yet.',
            action: CONNECT,
            missing: ['hevy_connected'],
          }),
        });
        const notice = el.querySelector('app-setup-notice section');

        expect(notice?.getAttribute('data-state')).toBe('setup_required');
        expect(el.querySelector('app-setup-notice .eyebrow')?.textContent).toContain('Welcome');
        expect(el.textContent).toContain('Hevy is not connected');
        expect(el.querySelector('app-setup-notice a.btn')?.getAttribute('href')).toBe('/settings');
        expect(el.textContent).not.toContain(surface.content);
      });

      it('says a sync is running and still shows the data that exists', async () => {
        const { el } = await render(surface, {
          ...surface.withData,
          setup: setup('syncing', {
            reason: 'A Hevy sync started 2 minutes ago is still running.',
          }),
        });
        const notice = el.querySelector('app-setup-notice section');

        expect(notice?.getAttribute('data-state')).toBe('syncing');
        expect(notice?.getAttribute('role')).toBe('status');
        expect(notice?.getAttribute('aria-busy')).toBe('true');
        expect(el.textContent).toContain('still running');
        expect(el.textContent).toContain(surface.content);
        // A running sync offers no setup step that would collide with it.
        expect(el.querySelector('app-setup-notice a.btn')).toBeNull();
      });

      it('raises an alert with the repair when the connection is degraded', async () => {
        const { el } = await render(surface, {
          ...surface.withData,
          setup: setup('degraded', {
            reason: 'Your last Hevy sync failed on 2026-09-01.',
            reason_code: 'sync_failed',
            action: RETRY,
          }),
        });
        const notice = el.querySelector('app-setup-notice section');

        expect(notice?.getAttribute('data-state')).toBe('degraded');
        expect(notice?.getAttribute('role')).toBe('alert');
        expect(el.querySelector('app-setup-notice a.btn')?.textContent).toContain(
          'Retry Hevy sync',
        );
        expect(el.textContent).toContain(surface.content);
      });

      it('flags out-of-date data beside the data', async () => {
        const { el } = await render(surface, {
          ...surface.withData,
          setup: setup('stale', {
            reason: 'Your last training activity was 30 days ago.',
            stale: true,
          }),
        });

        expect(el.querySelector('app-setup-notice section')?.getAttribute('data-state')).toBe(
          'stale',
        );
        expect(el.textContent).toContain(surface.content);
      });

      it('shows content with no notice when everything is ready', async () => {
        const { el } = await render(surface, {
          ...surface.withData,
          setup: setup('ready'),
        });

        expect(el.querySelector('app-setup-notice')).toBeNull();
        expect(el.textContent).toContain(surface.content);
      });

      it('shows a retryable error, not an empty account, when the request fails', async () => {
        await TestBed.configureTestingModule({
          imports: [surface.component],
          providers: [provideHttpClient(), provideHttpClientTesting()],
        }).compileComponents();
        const http = TestBed.inject(HttpTestingController);
        const fixture = TestBed.createComponent(surface.component);
        fixture.detectChanges();
        http.expectOne(surface.url).flush({}, { status: 503, statusText: 'Unavailable' });
        await fixture.whenStable();
        fixture.detectChanges();
        const el = fixture.nativeElement as HTMLElement;

        const error = el.querySelector('.error-panel');
        expect(error?.getAttribute('role')).toBe('alert');
        const retry = Array.from(el.querySelectorAll('button')).find((button) =>
          button.textContent?.includes('Try again'),
        );
        expect(retry).toBeTruthy();
        expect(el.querySelector('app-setup-notice')).toBeNull();

        retry?.click();
        http.expectOne(surface.url).flush({ ...surface.withData, setup: setup('ready') });
        await fixture.whenStable();
        fixture.detectChanges();
        expect(el.querySelector('.error-panel')).toBeNull();
        expect(el.textContent).toContain(surface.content);
        http.verify();
      });
    });
  }
});
