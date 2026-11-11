import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Settings } from './settings';
import { SettingsResponse } from './settings.types';

const providers = [
  { id: 'gemini', name: 'Google Gemini', default_model: 'gemini-3.8-flash' },
  { id: 'claude', name: 'Anthropic Claude', default_model: 'claude-sonnet-4-20250514' },
  { id: 'openai', name: 'OpenAI', default_model: 'gpt-4o' },
  { id: 'deepseek', name: 'DeepSeek', default_model: 'deepseek-chat' },
];

const settingsResponse: SettingsResponse = {
  user_prefs: {
    preferred_ai: 'claude',
    ai_model: 'claude-selected',
    goals: ['strength'],
    experience_level: 'advanced',
    constraints: [],
    coaching_style: 'direct',
    custom_rules: [],
  },
  user_keys: {
    hevy: { has_key: false, masked: null, model: null },
    gemini: { has_key: false, masked: null, model: null },
    claude: { has_key: true, masked: '••••••••key1', model: 'claude-stored' },
    openai: { has_key: false, masked: null, model: null },
    deepseek: { has_key: false, masked: null, model: null },
  },
  hevy_connection: {
    state: 'not_connected',
    account_username: null,
    account_id: null,
    api_version: null,
    capabilities: {},
    tested_at: null,
  },
  credential_vault: { available: true, reason: null },
  credential_events: [],
  ai_providers: providers,
  training_rules: {
    version: 2,
    prohibited_exercises: [{ exercise: 'Behind The Neck Press', reason: 'Shoulder impingement' }],
    substitutions: [
      { from_exercise: 'Barbell Back Squat', to_exercise: 'Leg Press', reason: 'Knee strain' },
    ],
    rep_intensity_bounds: { min_reps: 5, max_reps: 20, max_rpe: 8.5, min_rir: 1.5 },
    frequency: { max_sessions_per_week: 4, min_rest_days_between_sessions: 1 },
    volume: { max_sets_per_workout: 18, max_sets_per_exercise: 4 },
    equipment: {
      available_equipment: ['barbell', 'dumbbells'],
      prohibited_equipment: ['smith_machine'],
    },
    recovery: { min_sleep_hours: 7.5, min_readiness_score: 65, auto_deload_on_fatigue: true },
    contextual_notes: [
      { category: 'joint', note: 'Right shoulder sensitivity', severity: 'warning' },
    ],
  },
  gh_connected: false,
  gh_configured: false,
  connectors_info: [],
  vapid_public_key: null,
};

const connectedHevyResponse: SettingsResponse = {
  ...settingsResponse,
  user_keys: {
    ...settingsResponse.user_keys,
    hevy: { has_key: true, masked: '••••••••key9', model: null },
  },
  hevy_connection: {
    state: 'connected',
    account_username: 'lifter42',
    account_id: 'acct-1',
    api_version: 'v1',
    capabilities: { workouts: true, routines: false },
    tested_at: '2026-11-05T10:00:00+00:00',
  },
};

describe('Settings AI provider parity', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Settings],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function loadSettings(
    response: SettingsResponse = settingsResponse,
  ): Promise<ComponentFixture<Settings>> {
    const fixture = TestBed.createComponent(Settings);
    fixture.detectChanges();
    http.expectOne('/api/settings').flush(response);
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  it('renders every provider and default model supplied by the registry API', async () => {
    const fixture = await loadSettings();
    const element = fixture.nativeElement as HTMLElement;
    const labels = Array.from(element.querySelectorAll('.provider-tab')).map((button) =>
      button.textContent?.replace(/\s+/g, ' ').trim(),
    );

    expect(labels).toEqual(['✦ Google Gemini', '⬡ Anthropic Claude', '◉ OpenAI', '◉ DeepSeek']);
    expect(element.querySelector<HTMLInputElement>('#deepseek-model')?.placeholder).toBe(
      'deepseek-chat',
    );
    expect(element.querySelector<HTMLInputElement>('#claude-model')?.value).toBe('claude-selected');
  });

  it('persists the selected provider and its current model preference', async () => {
    const fixture = await loadSettings();
    const component = fixture.componentInstance;
    component.selectProvider('openai');
    component.aiModelInputs['openai'] = 'gpt-synthetic';

    component.savePreferences();

    const request = http.expectOne('/api/settings/preferences');
    expect(request.request.method).toBe('POST');
    expect(request.request.body.preferred_ai).toBe('openai');
    expect(request.request.body.ai_model).toBe('gpt-synthetic');
    request.flush({ status: 'ok' });
  });
  it('does not discard unsaved profile edits after saving a connector key', async () => {
    const fixture = await loadSettings();
    const component = fixture.componentInstance;
    component.constraintsInput = 'Keep this unsaved change';
    component.selectedGoals.set(['power']);
    component.hevyKeyInput = 'synthetic-test-key';
    component.saveKey('hevy');
    http.expectOne('/api/settings/key').flush({ status: 'ok' });
    http.expectOne('/api/settings').flush(settingsResponse);
    expect(component.constraintsInput).toBe('Keep this unsaved change');
    expect(component.selectedGoals()).toEqual(['power']);
    expect(component.hevyKeyInput).toBe('');
  });


  it('tests a stored credential through the vault endpoint and refreshes status', async () => {
    const fixture = await loadSettings();
    const component = fixture.componentInstance;
    component.testKey('hevy');

    const request = http.expectOne('/api/settings/key/test');
    expect(request.request.method).toBe('POST');
    expect(request.request.body.provider).toBe('hevy');
    request.flush({ status: 'ok', provider: 'hevy', detail: 'Connected.' });
    http.expectOne('/api/settings').flush(settingsResponse);
    expect(component.hevyStatus()).toContain('Connected');
  });

  it('explains unsupported notifications instead of silently failing', async () => {
    const fixture = await loadSettings();
    await fixture.componentInstance.subscribeToPush();
    expect(fixture.componentInstance.pushStatus()).toContain('not supported');
    expect(fixture.componentInstance.pushBusy()).toBe(false);
  });

  it('disconnects Google Health through HttpClient so the CSRF interceptor is used', async () => {
    const fixture = await loadSettings();
    fixture.componentInstance.disconnectGoogleHealth();
    fixture.componentInstance.disconnectGoogleHealth();
    const request = http.expectOne('/google-health/disconnect');
    expect(request.request.method).toBe('POST');
    expect(request.request.responseType).toBe('text');
    request.flush('');
    http.expectOne('/api/settings').flush(settingsResponse);
    expect(fixture.componentInstance.healthStatus()).toContain('disconnected');
  });

  it('loads and displays structured training rules in the accessible editor', async () => {
    const fixture = await loadSettings();
    const component = fixture.componentInstance;
    expect(component.trainingRules()?.version).toBe(2);
    expect(component.prohibitedExercises().length).toBe(1);
    expect(component.prohibitedExercises()[0].exercise).toBe('Behind The Neck Press');
    expect(component.substitutions().length).toBe(1);
    expect(component.substitutions()[0].to_exercise).toBe('Leg Press');
    expect(component.repBounds().min_reps).toBe(5);
    expect(component.repBounds().max_reps).toBe(20);
    expect(component.volume().max_sets_per_workout).toBe(18);
    expect(component.equipment().available).toContain('barbell');
    expect(component.recovery().min_sleep_hours).toBe(7.5);
    expect(component.contextualNotes().length).toBe(1);

    const element = fixture.nativeElement as HTMLElement;
    expect(element.querySelector('#rule-version-badge')?.textContent).toContain('v2');
  });

  it('allows adding and removing prohibited exercises, substitutions, and notes', async () => {
    const fixture = await loadSettings();
    const component = fixture.componentInstance;

    component.addProhibitedExercise();
    expect(component.prohibitedExercises().length).toBe(2);
    component.removeProhibitedExercise(0);
    expect(component.prohibitedExercises().length).toBe(1);

    component.addSubstitution();
    expect(component.substitutions().length).toBe(2);
    component.removeSubstitution(0);
    expect(component.substitutions().length).toBe(1);

    component.addContextualNote();
    expect(component.contextualNotes().length).toBe(2);
    component.removeContextualNote(0);
    expect(component.contextualNotes().length).toBe(1);
  });

  it('saves structured training rules via dedicated rules endpoint', async () => {
    const fixture = await loadSettings();
    const component = fixture.componentInstance;

    component.saveTrainingRules();

    const request = http.expectOne('/api/settings/rules');
    expect(request.request.method).toBe('POST');
    expect(request.request.body.version).toBe(2);
    expect(request.request.body.prohibited_exercises[0].exercise).toBe('Behind The Neck Press');
    expect(request.request.body.substitutions[0].from_exercise).toBe('Barbell Back Squat');

    request.flush({
      status: 'ok',
      rules: {
        ...request.request.body,
        version: 3,
      },
    });

    expect(component.rulesStatus()).toContain('Rules saved (v3)');
    expect(component.trainingRules()?.version).toBe(3);
  });

  it('shows active programme violations and remediation after conflicting rules', async () => {
    const fixture = await loadSettings();
    const component = fixture.componentInstance;

    component.saveTrainingRules();

    const request = http.expectOne('/api/settings/rules');
    request.flush({
      status: 'ok',
      rules: {
        ...request.request.body,
        version: 3,
      },
      blocking_violations: [
        {
          exercise: 'Bench Press',
          prescription: '8-12 reps',
          rule: 'Structured rule: minimum 12 reps',
          permitted_remediation: 'Increase prescribed rep range to at least 12 reps.',
        },
      ],
      reconciliation: {
        status: 'requires_remediation',
        message: 'The active programme contradicts the saved training rules.',
      },
    });
    await fixture.whenStable();
    fixture.detectChanges();

    expect(component.ruleViolations().length).toBe(1);
    expect(component.rulesStatus()).toContain('contradicts the saved training rules');
    const element = fixture.nativeElement as HTMLElement;
    expect(element.textContent).toContain('Bench Press');
    expect(element.textContent).toContain('Permitted remediation:');
  });

  it('includes structured rules in savePreferences profile payload', async () => {
    const fixture = await loadSettings();
    const component = fixture.componentInstance;

    component.savePreferences();

    const request = http.expectOne('/api/settings/preferences');
    expect(request.request.method).toBe('POST');
    expect(request.request.body.training_rules).toBeDefined();
    expect(request.request.body.training_rules.version).toBe(2);
    expect(request.request.body.training_rules.rep_intensity_bounds.min_reps).toBe(5);
    request.flush({ status: 'ok' });
  });

  it('verifies the stored Hevy credential without submitting a key', async () => {
    const fixture = await loadSettings();
    const component = fixture.componentInstance;

    component.verifyHevy();

    const request = http.expectOne('/api/settings/verify-hevy');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({});
    request.flush({
      status: 'ok',
      state: 'connected',
      account_username: 'lifter42',
      api_version: 'v1',
      capabilities: { workouts: true, routines: false },
      tested_at: '2026-11-05T10:00:00+00:00',
      workout_count: 42,
    });
    http.expectOne('/api/settings').flush(connectedHevyResponse);
    await fixture.whenStable();
    fixture.detectChanges();

    expect(component.hevyStatus()).toContain('Connected as lifter42');
    expect(component.hevyStatus()).toContain('API v1');
  });

  it('reports a rejected Hevy credential instead of a connection', async () => {
    const fixture = await loadSettings();
    const component = fixture.componentInstance;

    component.verifyHevy();

    const request = http.expectOne('/api/settings/verify-hevy');
    request.flush(
      { detail: 'Hevy rejected this API key. Generate a new key and try again.' },
      { status: 400, statusText: 'Bad Request' },
    );

    expect(component.hevyStatus()).toContain('rejected');
    expect(component.hevyStatus()).not.toContain('Connected');
  });

  it('keeps an unverified Hevy key out of storage and reports the rejection', async () => {
    const fixture = await loadSettings();
    const component = fixture.componentInstance;
    component.hevyKeyInput = 'synthetic-bad-key';

    component.saveKey('hevy');

    const request = http.expectOne('/api/settings/key');
    expect(request.request.body.provider).toBe('hevy');
    request.flush(
      { detail: 'Hevy rejected this API key. Generate a new key and try again.' },
      { status: 400, statusText: 'Bad Request' },
    );

    expect(component.hevyStatus()).toContain('rejected');
    // The rejected credential is not treated as a saved connection.
    expect(component.hevyKeyInput).toBe('synthetic-bad-key');
  });

  it('tests a typed Hevy key without storing it', async () => {
    const fixture = await loadSettings();
    const component = fixture.componentInstance;
    component.hevyKeyInput = 'synthetic-candidate-key';

    component.verifyHevy();

    const request = http.expectOne('/api/settings/verify-hevy');
    expect(request.request.body.api_key).toBe('synthetic-candidate-key');
    request.flush({
      status: 'ok',
      state: 'connected',
      account_username: 'lifter42',
      api_version: 'v1',
      capabilities: { workouts: true },
      tested_at: '2026-11-05T10:00:00+00:00',
    });
    await fixture.whenStable();
    fixture.detectChanges();

    expect(component.hevyStatus()).toContain('Connected as lifter42');
    // The tested key stays in the form so it can be saved without retyping.
    expect(component.hevyKeyInput).toBe('synthetic-candidate-key');
    // Testing a candidate never activates it, so no connection is recorded.
    expect(component.hevyConnection()?.state).toBe('not_connected');
  });

  it('shows the verified Hevy account returned by the settings API', async () => {
    const fixture = await loadSettings(connectedHevyResponse);
    const element = fixture.nativeElement as HTMLElement;
    const connection = element.querySelector('#hevy-connection')?.textContent ?? '';

    expect(connection).toContain('lifter42');
    expect(connection).toContain('API v1');
    expect(element.querySelector('.status.on')?.textContent).toContain('Connected');
  });

  it('flags a stored Hevy key that Hevy rejected', async () => {
    const rejectedResponse: SettingsResponse = {
      ...connectedHevyResponse,
      hevy_connection: {
        ...connectedHevyResponse.hevy_connection,
        state: 'invalid',
        account_username: null,
        tested_at: '2026-11-06T09:00:00+00:00',
      },
    };
    const fixture = await loadSettings(rejectedResponse);
    const element = fixture.nativeElement as HTMLElement;

    expect(element.querySelector('.status.off')?.textContent).toContain('Needs attention');
    expect(element.querySelector('#hevy-connection')?.textContent).toContain('rejected');
  });

  it('flags a stored Hevy key that cannot read workouts', async () => {
    const scopedResponse: SettingsResponse = {
      ...connectedHevyResponse,
      hevy_connection: {
        ...connectedHevyResponse.hevy_connection,
        state: 'insufficient_scope',
        capabilities: { workouts: false },
      },
    };
    const fixture = await loadSettings(scopedResponse);
    const element = fixture.nativeElement as HTMLElement;

    expect(element.querySelector('.status.off')?.textContent).toContain('Needs attention');
    expect(element.querySelector('#hevy-connection')?.textContent).toContain('cannot read');
  });

  it('never describes an unconfirmed Hevy connection as connected', async () => {
    const fixture = await loadSettings();
    const component = fixture.componentInstance;

    expect(
      component.describeHevyConnection({
        state: 'invalid',
        account_username: 'lifter42',
        account_id: 'acct-1',
        api_version: 'v1',
        capabilities: { workouts: true },
        tested_at: '2026-11-05T10:00:00+00:00',
      }),
    ).toContain('Not connected');
  });
});

const pendingClosure = {
  request_id: 'req-closure-1',
  scope: 'account',
  state: 'pending',
  requested_at: '2026-11-10T12:00:00+00:00',
  execute_after: '2026-11-17T12:00:00+00:00',
  completed_at: null,
  cancelled_at: null,
  can_cancel: true,
  attempts: 0,
  error_code: null,
  steps: [
    {
      step: 'notifications',
      label: 'Delete notifications',
      state: 'pending',
      rows_affected: 0,
      remaining_after: null,
    },
  ],
  progress_percent: 0,
};

const deletionInfo = (request: unknown = null, eligible = true) => ({
  eligible,
  reason: eligible ? null : 'not_eligible',
  scopes: eligible
    ? [
        { scope: 'health_data', required_confirmation: 'DELETE HEALTH DATA', grace_days: 0 },
        { scope: 'account', required_confirmation: 'athlete@example.com', grace_days: 7 },
      ]
    : undefined,
  request,
});

describe('Settings account and data deletion', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Settings],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.history.pushState({}, '', '/');
    http.verify();
  });

  async function openAccount(info: object): Promise<ComponentFixture<Settings>> {
    const fixture = TestBed.createComponent(Settings);
    fixture.detectChanges();
    http.expectOne('/api/settings').flush(settingsResponse);
    await fixture.whenStable();
    fixture.detectChanges();
    fixture.componentInstance.selectSection('account');
    http.expectOne('/api/account/deletion').flush(info);
    fixture.detectChanges();
    return fixture;
  }

  const query = <T extends Element>(fixture: ComponentFixture<Settings>, selector: string) =>
    (fixture.nativeElement as HTMLElement).querySelector<T>(selector);

  function type(fixture: ComponentFixture<Settings>, selector: string, value: string): void {
    const input = query<HTMLInputElement>(fixture, selector)!;
    input.value = value;
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  it('does not read deletion status until the Account section is opened', async () => {
    const fixture = TestBed.createComponent(Settings);
    fixture.detectChanges();
    http.expectOne('/api/settings').flush(settingsResponse);
    await fixture.whenStable();
    fixture.detectChanges();
    http.expectNone('/api/account/deletion');
    expect(query(fixture, '#settings-account')?.hasAttribute('hidden')).toBe(true);
  });

  it('opens straight to the section from the Account page deep link', async () => {
    window.history.pushState({}, '', '/settings?section=account');
    const fixture = TestBed.createComponent(Settings);
    fixture.detectChanges();
    http.expectOne('/api/settings').flush(settingsResponse);
    http.expectOne('/api/account/deletion').flush(deletionInfo());
    expect(fixture.componentInstance.section()).toBe('account');
  });

  it('keeps both destructive actions disabled until the exact text is typed', async () => {
    const fixture = await openAccount(deletionInfo());
    const health = () => query<HTMLButtonElement>(fixture, '#deletion-health-submit')!;
    const account = () => query<HTMLButtonElement>(fixture, '#deletion-account-submit')!;

    expect(health().disabled).toBe(true);
    expect(account().disabled).toBe(true);

    type(fixture, '#deletion-health-input', 'delete health');
    expect(health().disabled).toBe(true);
    type(fixture, '#deletion-health-input', '  delete   HEALTH data ');
    expect(health().disabled).toBe(false);

    type(fixture, '#deletion-account-input', 'someone.else@example.com');
    expect(account().disabled).toBe(true);
    type(fixture, '#deletion-account-input', 'Athlete@Example.com');
    expect(account().disabled).toBe(false);
    expect(query(fixture, '#deletion-account-help')?.textContent).toContain('7 days');
  });

  it('labels the confirmation fields and explains what is retained', async () => {
    const fixture = await openAccount(deletionInfo());
    for (const id of ['deletion-health-input', 'deletion-account-input']) {
      expect(query(fixture, `label[for="${id}"]`)).not.toBeNull();
      expect(query(fixture, `#${id}`)?.getAttribute('aria-describedby')).toContain(
        id.replace('-input', '-help'),
      );
      expect(query(fixture, `#${id}`)?.getAttribute('placeholder')).toBeNull();
    }
    expect(query(fixture, '#deletion-health-hint')?.textContent).toContain('DELETE HEALTH DATA');
    expect(query(fixture, '#deletion-account-hint')?.textContent).toContain('athlete@example.com');
    expect(query(fixture, '#deletion-status')?.getAttribute('role')).toBe('status');
    const text = (query(fixture, '#deletion-account') as HTMLElement).textContent ?? '';
    expect(text).toContain('anonymous record');
    expect(text).toContain('backups');
  });

  it('erases health data immediately with the typed confirmation', async () => {
    const fixture = await openAccount(deletionInfo());
    type(fixture, '#deletion-health-input', 'DELETE HEALTH DATA');

    fixture.componentInstance.requestDeletion('health_data');

    const request = http.expectOne('/api/account/deletion');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({
      scope: 'health_data',
      confirmation: 'DELETE HEALTH DATA',
    });
    request.flush({
      ...pendingClosure,
      request_id: 'req-health-1',
      scope: 'health_data',
      state: 'completed',
      completed_at: '2026-11-10T12:00:01+00:00',
      progress_percent: 100,
    });
    fixture.detectChanges();

    expect(query(fixture, '.key-status.success')?.textContent).toContain('req-health-1');
    expect(fixture.componentInstance.healthConfirmation).toBe('');
    expect(fixture.componentInstance.deletionStatus()).toContain('health data has been erased');
  });

  it('refuses to send a request whose confirmation does not match', async () => {
    const fixture = await openAccount(deletionInfo());
    fixture.componentInstance.accountConfirmation = 'nope';
    fixture.componentInstance.requestDeletion('account');
    http.expectNone('/api/account/deletion');
  });

  it('schedules an account closure, shows the window and lets the user cancel it', async () => {
    const fixture = await openAccount(deletionInfo());
    fixture.componentInstance.accountConfirmation = 'athlete@example.com';

    fixture.componentInstance.requestDeletion('account');
    http
      .expectOne('/api/account/deletion')
      .flush(pendingClosure, { status: 202, statusText: 'Accepted' });
    fixture.detectChanges();

    const panel = query<HTMLElement>(fixture, '#deletion-request')!;
    expect(panel.textContent).toContain('req-closure-1');
    expect(panel.textContent).toContain('paused');
    expect(query(fixture, '#deletion-health')).toBeNull(); // new requests are blocked
    const cancel = query<HTMLButtonElement>(fixture, '#deletion-cancel')!;
    expect(cancel.disabled).toBe(false);

    cancel.click();
    const response = http.expectOne('/api/account/deletion/cancel');
    expect(response.request.method).toBe('POST');
    response.flush({ ...pendingClosure, state: 'cancelled', can_cancel: false, steps: [] });
    fixture.detectChanges();

    expect(fixture.componentInstance.deletionStatus()).toContain('cancelled');
    expect(query(fixture, '#deletion-request')).toBeNull();
    expect(query(fixture, '#deletion-account-submit')).not.toBeNull();
  });

  it('disables cancellation once the window has closed', async () => {
    const fixture = await openAccount(deletionInfo({ ...pendingClosure, can_cancel: false }));
    expect(query<HTMLButtonElement>(fixture, '#deletion-cancel')!.disabled).toBe(true);
  });

  it('shows running erasure with per-step progress and keeps polling until it ends', async () => {
    // Capture only the 2s progress poll; every other timer (Angular's own) runs normally.
    const realSetTimeout = globalThis.setTimeout;
    const polls: (() => void)[] = [];
    vi.spyOn(globalThis, 'setTimeout').mockImplementation(((
      handler: TimerHandler,
      ms?: number,
      ...args: unknown[]
    ) => {
      if (ms === 2000 && typeof handler === 'function') {
        polls.push(handler as () => void);
        return 0;
      }
      return realSetTimeout(handler, ms, ...args);
    }) as unknown as typeof setTimeout);
    const running = {
      ...pendingClosure,
      state: 'in_progress',
      can_cancel: false,
      progress_percent: 50,
      steps: [
        {
          step: 'notifications',
          label: 'Delete notifications',
          state: 'done',
          rows_affected: 2,
          remaining_after: 0,
        },
        {
          step: 'caches',
          label: 'Clear server-side caches',
          state: 'pending',
          rows_affected: 0,
          remaining_after: null,
        },
      ],
    };
    const fixture = await openAccount(deletionInfo(running));

    const bar = query<HTMLProgressElement>(fixture, '.deletion-progress')!;
    expect(bar.value).toBe(50);
    expect(bar.getAttribute('aria-label')).toBe('Deletion progress');
    const steps = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('.deletion-steps li'),
    );
    expect(steps.map((item) => item.classList.contains('done'))).toEqual([true, false]);
    expect(steps[0].textContent).toContain('(done)');
    expect(query(fixture, '#deletion-cancel')).toBeNull();

    expect(polls).toHaveLength(1);
    polls[0]();
    http.expectOne('/api/account/deletion').flush(
      deletionInfo({
        ...running,
        progress_percent: 100,
        state: 'completed',
        scope: 'health_data',
      }),
    );
    expect(polls).toHaveLength(1); // finished: no further poll was scheduled
  });

  it('reports a failed erasure as retrying, not as success', async () => {
    const fixture = await openAccount(
      deletionInfo({
        ...pendingClosure,
        state: 'failed',
        can_cancel: false,
        error_code: 'residual_data',
      }),
    );
    const panel = query<HTMLElement>(fixture, '#deletion-request')!;
    expect(panel.textContent).toContain('retried automatically');
    expect(panel.textContent).toContain('residual_data');
    expect(query(fixture, '#deletion-account-submit')).toBeNull();
  });

  it('signs out in this browser after the account has been closed', async () => {
    const fixture = await openAccount(deletionInfo());
    const navigate = vi
      .spyOn(fixture.componentInstance as any, 'navigate')
      .mockImplementation(() => {});
    fixture.componentInstance.accountConfirmation = 'athlete@example.com';

    fixture.componentInstance.requestDeletion('account');
    http.expectOne('/api/account/deletion').flush({
      ...pendingClosure,
      state: 'completed',
      progress_percent: 100,
      completed_at: '2026-11-10T12:00:01+00:00',
    });
    await fixture.whenStable();
    await Promise.resolve();

    expect(navigate).toHaveBeenCalledWith('/logout');
    expect(fixture.componentInstance.deletionStatus()).toContain('closed');
  });

  it('treats a refused session on the status endpoint as a completed closure', async () => {
    const fixture = TestBed.createComponent(Settings);
    fixture.detectChanges();
    http.expectOne('/api/settings').flush(settingsResponse);
    await fixture.whenStable();
    const navigate = vi
      .spyOn(fixture.componentInstance as any, 'navigate')
      .mockImplementation(() => {});

    fixture.componentInstance.selectSection('account');
    http
      .expectOne('/api/account/deletion')
      .flush({ detail: 'Unauthorized' }, { status: 401, statusText: 'Unauthorized' });
    await fixture.whenStable();

    expect(navigate).toHaveBeenCalledWith('/logout');
  });

  it.each([
    [400, 'confirmation_mismatch', 'did not match'],
    [409, 'already_requested', 'already open'],
    [403, 'not_eligible', 'cannot be deleted'],
    [423, 'locked', 'being erased right now'],
    [500, 'boom', 'could not be completed'],
  ])('explains a %i response without exposing internals', async (status, detail, expected) => {
    const fixture = await openAccount(deletionInfo());
    fixture.componentInstance.healthConfirmation = 'DELETE HEALTH DATA';

    fixture.componentInstance.requestDeletion('health_data');
    http.expectOne('/api/account/deletion').flush({ detail }, { status, statusText: 'Error' });
    fixture.detectChanges();

    expect(query(fixture, '#deletion-error')?.getAttribute('role')).toBe('alert');
    expect(fixture.componentInstance.deletionError()).toContain(expected);
    expect(fixture.componentInstance.deletionBusy()).toBe(false);
  });

  it('offers no deletion controls to an account that is not eligible', async () => {
    const fixture = await openAccount(deletionInfo(null, false));
    expect(query(fixture, '#deletion-health')).toBeNull();
    expect(query(fixture, '#deletion-account')).toBeNull();
    expect((query(fixture, '#settings-account') as HTMLElement).textContent).toContain(
      'signed in with your own account',
    );
  });
});
