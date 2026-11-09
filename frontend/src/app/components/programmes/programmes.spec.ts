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

  it('surfaces Hevy source drift and lets the user keep the active programme', () => {
    component.data.set({
      source_drift: {
        status: 'action_required',
        has_changes: true,
        routines: [{ routine_id: 'routine-a', title: 'Upper A', state: 'removed' }],
      },
    });
    expect(component.driftNeedsAttention()).toBe(true);
    expect(component.sourceDrift()?.status).toBe('action_required');

    component.keepCurrentProgramme();

    expect(component.sourceDrift()).toBeNull();
    expect(component.status()).toContain('Keeping the active programme');
  });

  it('asks the user to reconnect when the Hevy source cannot be verified', () => {
    component.data.set({
      source_drift: { status: 'source_unavailable', has_changes: false, routines: [] },
    });
    expect(component.driftNeedsAttention()).toBe(true);
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
