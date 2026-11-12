import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Programmes } from './programmes';

function routine(id: string, title: string, folderId: number | null = 1, folderName = 'Current split') {
  return {
    id,
    title,
    folder_id: folderId,
    folder_name: folderName,
    exercise_count: 2,
    work_set_count: 6,
    estimated_duration_minutes: 45,
    muscle_summary: ['chest'],
    updated_at: '2026-08-18T12:00:00Z',
    recent_completion_count: 2,
    last_performed_at: '2026-08-19T10:00:00Z',
    unsupported_fields: ['rpe'],
    source_hash: `${id}-hash`,
    exercises: [
      {
        template_id: 'bench',
        title: 'Bench Press',
        sets: 3,
        exercise_type: 'weight_reps',
        primary_muscle_group: 'chest',
        secondary_muscle_groups: ['triceps'],
        set_targets: [{ type: 'normal', reps: 8, weight_kg: 60 }],
        set_summary: [
          {
            index: 1,
            type: 'normal',
            load_kg: 60,
            reps: 8,
            rep_range: null,
            distance_meters: null,
            duration_seconds: null,
            rpe: 9,
            custom_targets: { rpe: 9 },
            unsupported_fields: ['rpe'],
          },
        ],
        set_types: ['normal'],
        unsupported_fields: ['rpe'],
        notes: null,
        rest_seconds: 120,
        superset_id: null,
      },
    ],
  };
}

function builderPayload(overrides: any = {}) {
  return {
    active_programme: null,
    templates: [],
    connection: {
      state: 'connected',
      detail: null,
      username: 'tester',
      owner: 'athlete@example.test',
      last_successful_sync: '2026-08-20T09:00:00+00:00',
      stale: false,
      warnings: [],
      counts: { routines: 2, folders: 1 },
    },
    source: {
      username: 'tester',
      workout_count: 42,
      routine_count: 2,
      recent_workout_count: 8,
      folders: [{ id: 1, title: 'Current split', routine_count: 2 }],
      routines: [routine('routine-a', 'Upper A'), routine('routine-b', 'Lower B')],
    },
    duration: { minimum_weeks: 4, maximum_weeks: 52, default_weeks: 12 },
    goals: [{ key: 'general_fitness', label: 'General strength and fitness', description: 'x' }],
    cadence: { modes: [], weekdays: [], roles: [] },
    ...overrides,
  };
}

function previewPayload(overrides: any = {}) {
  return {
    name: 'General strength and fitness · Hevy routines',
    engine_version: 'hevy-programme-v2',
    preview_token: 'token-1234567890',
    cycle_weeks: 12,
    total_days: 2,
    analysis: { confidence: 'medium' },
    cadence: {
      mode: 'rolling',
      sessions_per_week: 7,
      training_day_names: ['Monday'],
      rotation: [],
      unscheduled_routines: [],
    },
    schedule: [{ date: '2026-09-01', weekday: 'Tuesday', routine_title: 'Upper A' }],
    muscle_sets: {
      method: 'Direct sets count as 1.0.',
      weekly_sets: { chest: 3 },
      estimates: [{ muscle: 'chest', weekly_sets: 3, status: 'below_reference_range' }],
      warnings: [],
    },
    drift: { state: 'in_sync', changed_routines: [] },
    blocks: [
      {
        number: 1,
        key: 'foundation',
        name: 'Foundation',
        weeks: '1-6',
        start_date: '2026-09-01',
        end_date: '2026-10-12',
        focus: 'Establish a baseline.',
        volume_multiplier: 0.95,
        target_rir: '2-4',
        rep_emphasis: 'moderate reps',
      },
    ],
    days: [
      {
        number: 1,
        routine_id: 'routine-a',
        focus: 'Upper A',
        muscles: ['chest'],
        estimated_duration_minutes: 45,
        exercises: [
          {
            name: 'Bench Press',
            role: 'primary_strength',
            role_reason: 'First loaded movement.',
            scheme: '3 × 4-8',
            prescriptions: [
              {
                block_number: 1,
                sets: 3,
                rep_range: '4-8',
                target_rir: '2-4',
                progression: { method: 'autoregulated_rep_range', rule: 'Add load after two hits.' },
              },
            ],
          },
        ],
      },
    ],
    source_snapshots: [{ id: 'routine-a', title: 'Upper A', source_hash: 'routine-a-hash' }],
    warnings: ['Training-history confidence is low.'],
    blocking_violations: [],
    assumptions: ['Original Hevy routines remain read-only.'],
    ...overrides,
  };
}

describe('Hevy-first programme builder', () => {
  let fixture: any;
  let component: Programmes;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [Programmes],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    fixture = TestBed.createComponent(Programmes);
    component = fixture.componentInstance;
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('shows the Hevy state, the routine library and no static programme language', () => {
    fixture.detectChanges();
    http.expectOne('/api/programmes').flush(builderPayload());
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent as string;
    expect(text).toContain('Hevy state');
    expect(text).toContain('Signed in as');
    expect(text).toContain('athlete@example.test');
    expect(text).toContain('Last successful sync');
    expect(text).toContain('Upper A');
    expect(text).toContain('Lower B');
    expect(text).not.toContain('Hybrid Powerbuilding');
    expect(text).not.toContain('Available Templates');
    expect(text).not.toContain('Active Split Preview');
  });

  it('groups routines by Hevy folder and preserves the provider order', () => {
    fixture.detectChanges();
    http.expectOne('/api/programmes').flush(
      builderPayload({
        source: {
          username: 'tester',
          workout_count: 42,
          routine_count: 3,
          recent_workout_count: 8,
          folders: [
            { id: 1, title: 'Current split', routine_count: 2 },
            { id: 2, title: 'Archive', routine_count: 1 },
          ],
          routines: [
            routine('routine-a', 'Upper A', 1, 'Current split'),
            routine('routine-b', 'Lower B', 1, 'Current split'),
            routine('routine-c', 'Old Push', 2, 'Archive'),
          ],
        },
      }),
    );
    fixture.detectChanges();

    const groups = component.routineGroups();
    expect(groups.map((group) => group.title)).toEqual(['Current split', 'Archive']);
    expect(groups[0].routines.map((item) => item.id)).toEqual([
      'routine-a',
      'routine-b',
    ]);
    expect(groups[1].routines.map((item) => item.id)).toEqual(['routine-c']);
  });

  it('shows setup guidance instead of a programme when Hevy is disconnected', () => {
    fixture.detectChanges();
    http.expectOne('/api/programmes').flush(
      builderPayload({
        connection: {
          state: 'disconnected',
          detail: 'Add your Hevy API key in Settings to import routines.',
          username: null,
          owner: 'athlete@example.test',
          last_successful_sync: null,
          stale: true,
          warnings: [],
          counts: {},
        },
        source: {
          username: null,
          workout_count: 0,
          routine_count: 0,
          recent_workout_count: 0,
          folders: [],
          routines: [],
        },
      }),
    );
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent as string;
    expect(text).toContain('Connect Hevy first');
    expect(text).not.toContain('Available Templates');
  });

  it('keeps the selection stable when the source is refreshed', () => {
    fixture.detectChanges();
    http.expectOne('/api/programmes').flush(builderPayload());
    fixture.detectChanges();

    component.toggleRoutine('routine-a');
    component.loadProgrammes(true);
    http.expectOne('/api/programmes').flush(builderPayload());
    fixture.detectChanges();

    expect(component.selectedRoutineIds()).toEqual(['routine-a']);
  });

  it('reorders the selected routines with the keyboard', () => {
    fixture.detectChanges();
    http.expectOne('/api/programmes').flush(builderPayload());
    fixture.detectChanges();

    component.toggleRoutine('routine-a');
    component.toggleRoutine('routine-b');
    expect(component.selectedRoutineIds()).toEqual(['routine-a', 'routine-b']);

    component.onRoutineKeydown(
      new KeyboardEvent('keydown', { key: 'ArrowDown', ctrlKey: true }),
      0,
    );
    expect(component.selectedRoutineIds()).toEqual(['routine-b', 'routine-a']);

    component.onRoutineKeydown(
      new KeyboardEvent('keydown', { key: 'ArrowUp', ctrlKey: true }),
      1,
    );
    expect(component.selectedRoutineIds()).toEqual(['routine-a', 'routine-b']);
  });

  it('sends roles, cadence, duration and goal in the preview payload', () => {
    fixture.detectChanges();
    http.expectOne('/api/programmes').flush(builderPayload());
    fixture.detectChanges();

    component.toggleRoutine('routine-a');
    component.setRole('routine-a', 'rotating');
    component.setCadenceMode('weekly');
    component.toggleWeekday(1);
    component.durationWeeks = 8;
    component.goal = 'hypertrophy';

    const payload = component.buildPayload();
    expect(payload.selected_routine_ids).toEqual(['routine-a']);
    expect(payload.routine_selections).toEqual([
      { routine_id: 'routine-a', role: 'rotating' },
    ]);
    expect(payload.cadence.mode).toBe('weekly');
    expect(payload.cadence.weekdays).toEqual([0, 1, 2, 4]);
    expect(payload.duration_weeks).toBe(8);
    expect(payload.goal).toBe('hypertrophy');
    expect(payload.event_date).toBeNull();
    expect(payload.constraints.max_sessions_per_week).toBeNull();
  });

  it('discards a stale preview when the settings change', () => {
    fixture.detectChanges();
    http.expectOne('/api/programmes').flush(builderPayload());
    fixture.detectChanges();

    component.selectedRoutineIds.set(['routine-a']);
    component.preview.set(previewPayload());
    component.generatePreview();

    expect(component.preview()).toBeNull();
    const request = http.expectOne('/api/programmes/preview');
    component.durationWeeks = 8;
    request.flush({ preview: previewPayload() });

    expect(component.preview()).toBeNull();
    expect(component.status()).toContain('fresh preview');
  });

  it('renders the generated programme preview with blocks, muscle sets and prescriptions', () => {
    fixture.detectChanges();
    http.expectOne('/api/programmes').flush(builderPayload());
    fixture.detectChanges();

    component.selectedRoutineIds.set(['routine-a']);
    component.preview.set(previewPayload());

    fixture.detectChanges();
    const text = fixture.nativeElement.textContent as string;
    expect(text).toContain('Generated programme preview');
    expect(text).toContain('Block timeline');
    expect(text).toContain('Foundation');
    expect(text).toContain('Estimated weekly sets by muscle');
    expect(text).toContain('chest');
    expect(text).toContain('Training-history confidence is low.');
  });

  it('prevents duplicate activation submits and reports success', () => {
    fixture.detectChanges();
    http.expectOne('/api/programmes').flush(builderPayload());
    fixture.detectChanges();

    component.selectedRoutineIds.set(['routine-a']);
    component.preview.set(previewPayload());

    component.activatePreview();
    component.activatePreview();
    const request = http.expectOne('/api/programmes/activate');
    expect(request.request.body.preview_token).toBe('token-1234567890');
    expect(request.request.body.source_hashes).toEqual([
      { routine_id: 'routine-a', source_hash: 'routine-a-hash' },
    ]);
    request.flush({ status: 'ok' });

    expect(component.preview()).toBeNull();
    expect(component.status()).toContain('now active');
    http.expectOne('/api/programmes').flush(builderPayload());
  });

  it('surfaces source conflicts with remediation steps', () => {
    fixture.detectChanges();
    http.expectOne('/api/programmes').flush(builderPayload());
    fixture.detectChanges();

    component.selectedRoutineIds.set(['routine-a']);
    component.preview.set(previewPayload());
    component.activatePreview();

    http.expectOne('/api/programmes/activate').flush(
      {
        detail: {
          message: 'The Hevy routines or programme settings changed after the preview.',
          conflict: {
            changed_routines: [{ routine_id: 'routine-a', title: 'Upper A' }],
            added_routines: [],
            removed_routines: [],
            has_conflicts: true,
          },
          remediation: ['Refresh from Hevy to load the current routine revisions.'],
        },
      },
      { status: 409, statusText: 'Conflict' },
    );
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent as string;
    expect(text).toContain('Source changed since the preview');
    expect(text).toContain('Upper A');
    expect(text).toContain('Refresh from Hevy to load the current routine revisions.');
    expect(component.preview()).not.toBeNull();
  });

  it('surfaces Hevy source drift and lets the user keep the active programme', () => {
    component.data.set(
      builderPayload({
        source_drift: {
          status: 'action_required',
          has_changes: true,
          summary: { renamed: 0, content_changed: 0, removed: 1 },
          routines: [
            {
              position: 1,
              routine_id: 'routine-a',
              title: 'Upper A',
              current_title: null,
              state: 'removed',
              source_exercise_count: 2,
              current_exercise_count: null,
            },
          ],
        },
      }),
    );
    expect(component.driftNeedsAttention()).toBe(true);
    expect(component.sourceDrift()?.status).toBe('action_required');

    component.keepCurrentProgramme();

    expect(component.sourceDrift()).toBeNull();
    expect(component.status()).toContain('Keeping the active programme');
  });

  it('asks the user to reconnect when the Hevy source cannot be verified', () => {
    component.data.set(
      builderPayload({
        source_drift: {
          status: 'source_unavailable',
          has_changes: false,
          summary: { renamed: 0, content_changed: 0, removed: 0 },
          routines: [],
        },
      }),
    );
    expect(component.driftNeedsAttention()).toBe(true);
  });

  it('keeps selections across filtered and paginated routine lists', () => {
    component.data.set(
      builderPayload({
        source: {
          username: 'tester',
          workout_count: 0,
          routine_count: 30,
          recent_workout_count: 0,
          folders: [],
          routines: Array.from({ length: 30 }, (_, i) =>
            routine(`r${i}`, `Routine ${i}`, i % 2),
          ),
        },
      }),
    );
    component.toggleRoutine('r29');
    component.setSearch('Routine 29');
    component.filterFolder('1');
    const visible = component.routineGroups().flatMap((group) => group.routines);
    expect(visible.map((routine) => routine.id)).toEqual(['r29']);
    expect(component.isSelected('r29')).toBe(true);
  });

  it('surfaces blocking violations from activation', () => {
    fixture.detectChanges();
    http.expectOne('/api/programmes').flush(builderPayload());
    fixture.detectChanges();

    component.selectedRoutineIds.set(['routine-a']);
    component.preview.set(previewPayload());
    component.activatePreview();

    http.expectOne('/api/programmes/activate').flush(
      {
        detail: {
          message: 'The programme preview violates blocking constraints.',
          violations: [
            {
              code: 'routine_not_scheduled',
              field: 'cadence.weekdays',
              routine_id: 'routine-a',
              message: "The routine 'Upper A' is required but has no training day.",
            },
          ],
        },
      },
      { status: 422, statusText: 'Unprocessable' },
    );
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent as string;
    expect(text).toContain('Fix these before activating');
    expect(text).toContain("The routine 'Upper A' is required but has no training day.");
  });

  it('reports the refresh states for partial, auth and rate-limited reads', () => {
    fixture.detectChanges();
    http.expectOne('/api/programmes').flush(builderPayload());
    fixture.detectChanges();

    component.refreshFromHevy();
    http.expectOne('/api/programmes/refresh').flush({
      state: 'partial',
      warnings: ['Routine folders could not be read from Hevy: timeout'],
      counts: { routines: 1 },
      source: builderPayload().source,
      last_successful_sync: null,
      stale: true,
    });
    fixture.detectChanges();
    expect(component.status()).toContain('part of your account');

    component.refreshFromHevy();
    http.expectOne('/api/programmes/refresh').flush({
      state: 'auth_error',
      detail: 'Hevy rejected your API key. Update it in Settings and refresh.',
    });
    fixture.detectChanges();
    expect(component.error()).toContain('rejected your API key');

    component.refreshFromHevy();
    http.expectOne('/api/programmes/refresh').flush({
      state: 'rate_limited',
      detail: 'Hevy is rate limiting requests. Try again in a few minutes.',
    });
    fixture.detectChanges();
    expect(component.error()).toContain('rate limiting');
  });

  it('labels routines that changed since the active programme', () => {
    fixture.detectChanges();
    http.expectOne('/api/programmes').flush(
      builderPayload({
        active_programme: {
          source: 'hevy',
          definition: {
            name: 'Active programme',
            cycle_weeks: 12,
            total_days: 2,
            programme_spec: { goal_label: 'General fitness' },
            source_snapshots: [
              { id: 'routine-a', title: 'Upper A', source_hash: 'older-hash' },
            ],
          },
        },
      }),
    );
    fixture.detectChanges();

    expect(component.driftFor(component.routineById('routine-a'))).toBe('changed');
    expect(component.driftFor(component.routineById('routine-b'))).toBe('added');
    const text = fixture.nativeElement.textContent as string;
    expect(text).toContain('Source changed');
    expect(text).toContain('Not in the active programme');
  });

  it('labels every builder control for assistive technology', () => {
    fixture.detectChanges();
    http.expectOne('/api/programmes').flush(builderPayload());
    fixture.detectChanges();

    const element = fixture.nativeElement as HTMLElement;
    expect(element.querySelector('label[for="routine-search"]')).toBeTruthy();
    expect(element.querySelector('#folder-filter')).toBeTruthy();
    expect(element.querySelector('label[for="folder-filter"]')).toBeTruthy();

    component.toggleRoutine('routine-a');
    fixture.detectChanges();

    const checkbox = element.querySelector(
      'input[type="checkbox"][aria-label="Select routine Upper A"]',
    ) as HTMLInputElement;
    expect(checkbox).toBeTruthy();

    const moveButtons = Array.from(
      element.querySelectorAll('.order-buttons button'),
    ) as HTMLButtonElement[];
    expect(moveButtons.length).toBeGreaterThan(0);
    expect(
      moveButtons.every((button) => !!button.getAttribute('aria-label')),
    ).toBe(true);

    const roleSelect = element.querySelector(
      'select[aria-label="Role for Upper A"]',
    ) as HTMLSelectElement;
    expect(roleSelect).toBeTruthy();
  });

  it('blocks activation when preview contains blocking constraint violations', () => {
    component.preview.set(
      previewPayload({
        preview_token: 'violation-token',
        blocking_violations: [
          {
            exercise: 'Bulgarian Split Squats',
            prescription: '3 x 10',
            rule: 'No Bulgarian split squats',
            permitted_remediation: 'Use leg extensions or flat-foot leg press instead.',
          },
        ],
      }),
    );
    component.activatePreview();
    http.expectNone('/api/programmes/activate');
  });
});
