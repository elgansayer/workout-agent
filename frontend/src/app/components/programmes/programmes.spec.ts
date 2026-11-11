import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Programmes } from './programmes';

describe('Programme preview consistency', () => {
  let component: Programmes;
  let http: HttpTestingController;
  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    component = TestBed.runInInjectionContext(() => new Programmes());
    http = TestBed.inject(HttpTestingController);
    component.selectedRoutineIds.set(['routine-a']);
  });
  afterEach(() => http.verify());

  it('discards old previews and ignores responses for settings that have changed', () => {
    component.preview.set({ preview_token: 'old' });
    component.generatePreview();
    expect(component.preview()).toBeNull();
    component.activatePreview();
    http.expectNone('/api/programmes/activate');
    component.durationWeeks = 8;
    http.expectOne('/api/programmes/preview').flush({ preview: { preview_token: 'stale' } });
    expect(component.preview()).toBeNull();
    expect(component.status()).toContain('fresh preview');
  });

  it('preserves activation feedback and does not offer the same preview twice', () => {
    component.preview.set({ preview_token: 'current' });
    component.activatePreview();
    component.activatePreview();
    http.expectOne('/api/programmes/activate').flush({});
    expect(component.preview()).toBeNull();
    http.expectOne('/api/programmes').flush({ source: { routines: [{ id: 'routine-a' }] } });
    expect(component.status()).toBe('Your Hevy-derived programme is now active.');
  });

  it('keeps selections across filtered and paginated routine lists', () => {
    component.data.set({
      source: {
        routines: Array.from({ length: 30 }, (_, i) => ({
          id: `r${i}`,
          title: `Routine ${i}`,
          folder_id: i % 2,
        })),
      },
    });
    component.toggleRoutine('r29');
    component.routineList.next();
    component.filterFolder('1');
    component.routineList.search('Routine 29');
    expect(component.visibleRoutines().map((r) => r.id)).toEqual(['r29']);
    expect(component.isSelected('r29')).toBe(true);
  });
});

describe('Programme builder connection states', () => {
  const connection = (state: string, extra: Record<string, unknown> = {}) => ({
    connector: 'hevy',
    label: 'Hevy',
    state,
    reason: 'synthetic',
    message: `Hevy is ${state}.`,
    detail: `Hevy is ${state}.`,
    next_action: 'none',
    action_label: null,
    action_href: null,
    username: null,
    load_failed: false,
    ...extra,
  });

  function render(payload: object) {
    TestBed.configureTestingModule({
      imports: [Programmes],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    const fixture = TestBed.createComponent(Programmes);
    fixture.detectChanges();
    TestBed.inject(HttpTestingController).expectOne('/api/programmes').flush(payload);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  const source = { routines: [], folders: [], routine_count: 0, recent_workout_count: 0 };

  it('asks to connect Hevy when setup is required and nothing failed to load', () => {
    const el = render({ connection: connection('setup_required'), source });
    expect(el.textContent).toContain('Connect Hevy first');
    expect(el.textContent).not.toContain('Hevy source unavailable');
  });

  it('explains a rejected key instead of pretending Hevy is simply disconnected', () => {
    const el = render({
      connection: connection('setup_required', {
        reason: 'credential_rejected',
        load_failed: true,
        detail: 'Hevy rejected your credentials. Update them to continue.',
        action_label: 'Update Hevy credentials',
        action_href: '/settings?section=connections',
      }),
      source,
    });
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('rejected your credentials');
    expect(el.querySelector('a[href="/settings?section=connections"]')?.textContent).toContain(
      'Update Hevy credentials',
    );
    expect(el.textContent).not.toContain('Connect Hevy first');
  });

  it('shows a failed read even when sync readiness is still ready', () => {
    const el = render({
      connection: connection('ready', {
        load_failed: true,
        detail: 'Hevy is temporarily unavailable. Try again shortly.',
      }),
      source,
    });
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('temporarily unavailable');
  });

  it('keeps the builder usable while history is still connecting, with the shared notice', () => {
    const el = render({
      connection: connection('connecting', {
        reason: 'awaiting_first_sync',
        message: 'Hevy is connected. The first sync has not completed yet.',
        action_label: 'Sync now',
        action_href: '/settings?section=connections',
      }),
      source,
    });
    expect(el.querySelector('.status-notice[data-state="connecting"]')?.textContent).toContain(
      'first sync has not completed',
    );
    expect(el.textContent).toContain('No Hevy routines found');
  });

  it('tolerates a response without connection details', () => {
    const el = render({ source });
    expect(el.textContent).toContain('No Hevy routines found');
  });
});
