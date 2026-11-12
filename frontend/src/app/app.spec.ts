import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { App } from './app';
import { Auth } from './services/auth';
import { DEFAULT_DISPLAY_SETTINGS, DisplaySettingsService } from './services/display-settings';
import { SetupAccount, SetupStateService } from './services/setup-state.service';
import { SetupState, SetupStateKind } from './shared/setup-state';

describe('App', () => {
  const authStub = {
    currentUser: signal(null),
    checkAuth: () => undefined,
    logout: () => undefined,
  };

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [App],
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: Auth, useValue: authStub },
      ],
    }).compileComponents();
  });

  it("loads the signed-in user's display settings and applies them to the page", async () => {
    const fixture = TestBed.createComponent(App);
    TestBed.inject(HttpTestingController)
      .expectOne('/api/settings/display')
      .flush({
        settings: {
          ...DEFAULT_DISPLAY_SETTINGS,
          locale: 'en-US',
          motion: 'reduce',
          contrast: 'more',
          text_size: 'large',
        },
        options: null,
      });
    await fixture.whenStable();

    const root = document.documentElement;
    expect(TestBed.inject(DisplaySettingsService).settings().locale).toBe('en-US');
    expect(root.lang).toBe('en-US');
    expect(root.dataset['motion']).toBe('reduce');
    expect(root.dataset['contrast']).toBe('more');
    expect(root.dataset['textSize']).toBe('large');
    root.removeAttribute('lang');
    delete root.dataset['motion'];
    delete root.dataset['contrast'];
    delete root.dataset['textSize'];
  });

  it('should create the app', () => {
    const fixture = TestBed.createComponent(App);
    expect(fixture.componentInstance).toBeTruthy();
  });

  it('should render an accessible primary navigation and skip link', () => {
    const fixture = TestBed.createComponent(App);
    fixture.detectChanges();

    const compiled = fixture.nativeElement as HTMLElement;
    const navigation = compiled.querySelector('nav[aria-label="Primary navigation"]');
    const skipLink = compiled.querySelector<HTMLAnchorElement>('a.skip-to-content');

    expect(navigation).toBeTruthy();
    expect(navigation?.querySelectorAll('a').length).toBeGreaterThanOrEqual(9);
    expect(skipLink?.getAttribute('href')).toBe('#main-content');
    expect(compiled.querySelector('main')?.id).toBe('main-content');
  });

  describe('readiness badges', () => {
    function surface(state: SetupStateKind): SetupState {
      return {
        state,
        label: state,
        reason: '',
        action: null,
        missing: [],
        last_activity_at: null,
        age_days: null,
        stale: false,
      };
    }

    function account(state: SetupStateKind): SetupAccount {
      return {
        state,
        label: state,
        reason: '',
        reason_code: null,
        first_run: false,
        checks: {},
        missing: [],
        last_activity_at: null,
        age_days: null,
        stale: false,
        connection_state: null,
        connector: null,
        ai_provider_state: null,
      };
    }

    function link(compiled: HTMLElement, href: string): HTMLAnchorElement {
      const found = compiled.querySelector<HTMLAnchorElement>(`nav a[href="${href}"]`);
      if (!found) throw new Error(`Missing navigation link ${href}`);
      return found;
    }

    function badgeAfter(anchor: HTMLAnchorElement): HTMLElement | null {
      const next = anchor.nextElementSibling;
      return next?.classList.contains('nav-badge') ? (next as HTMLElement) : null;
    }

    it('shows each surface its own state, and names it for assistive technology', () => {
      const setup = TestBed.inject(SetupStateService);
      setup.surfaces.set({
        dashboard: surface('setup_required'),
        progress: surface('syncing'),
        stats: surface('degraded'),
        plan: surface('stale'),
        history: surface('ready'),
        checkins: surface('connecting'),
      });
      setup.account.set(account('degraded'));
      const fixture = TestBed.createComponent(App);
      fixture.detectChanges();
      const compiled = fixture.nativeElement as HTMLElement;

      const expected: [string, string | null, string | null][] = [
        ['/dashboard', 'Setup', 'Today (setup needed)'],
        ['/progress', 'Syncing', 'Progress (syncing)'],
        ['/stats', 'Attention', 'Stats (needs attention)'],
        ['/plan', 'Stale', 'Plan (data out of date)'],
        ['/history', null, null],
        ['/checkins', 'Connecting', 'Check-ins (connecting to Hevy)'],
        ['/settings', 'Attention', 'Settings (needs attention)'],
      ];
      for (const [href, label, name] of expected) {
        const anchor = link(compiled, href);
        expect(badgeAfter(anchor)?.textContent?.trim() ?? null, href).toBe(label);
        expect(anchor.getAttribute('aria-label'), href).toBe(name);
      }
      expect(badgeAfter(link(compiled, '/stats'))?.getAttribute('data-kind')).toBe('degraded');
    });

    it('only says training is built from connected data when the account is ready', () => {
      const setup = TestBed.inject(SetupStateService);
      const footer = (fixture: ComponentFixture<App>) =>
        fixture.nativeElement.querySelector('footer.foot').textContent.trim();

      const unknown = TestBed.createComponent(App);
      unknown.detectChanges();
      expect(footer(unknown)).not.toContain('built from your connected workout data');

      setup.account.set(account('degraded'));
      unknown.detectChanges();
      expect(footer(unknown)).toContain('once Hevy is connected and synced');

      setup.account.set(account('ready'));
      unknown.detectChanges();
      expect(footer(unknown)).toBe('Adaptive training built from your connected workout data.');
    });

    it('badges nothing for an account whose state is not known yet', () => {
      const fixture = TestBed.createComponent(App);
      fixture.detectChanges();
      const compiled = fixture.nativeElement as HTMLElement;

      expect(compiled.querySelectorAll('nav .nav-badge')).toHaveLength(0);
      expect(link(compiled, '/chat').getAttribute('aria-label')).toBeNull();
    });
  });
});
