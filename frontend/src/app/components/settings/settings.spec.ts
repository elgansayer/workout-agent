import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { DEFAULT_DISPLAY_SETTINGS, DisplayOptions } from '../../services/display-settings';
import { vi } from 'vitest';
import { SetupAccount, SetupStateService } from '../../services/setup-state.service';
import { Settings } from './settings';
import { SettingsResponse } from './settings.types';

const retentionOptions = [
  {
    mode: 'revoke_only',
    label: 'Revoke access only',
    explanation: 'Revoke the credential and keep imported data.',
    deletes_imported_data: false,
    deletes_derived_data: false,
  },
  {
    mode: 'retain',
    label: 'Disconnect and retain imported data',
    explanation: 'Disconnect but keep imported data and derived insights.',
    deletes_imported_data: false,
    deletes_derived_data: false,
  },
  {
    mode: 'purge',
    label: 'Disconnect and purge imported data',
    explanation: 'Delete imported data and derived insights, keeping your own records.',
    deletes_imported_data: true,
    deletes_derived_data: true,
  },
] as const;

const displayOptions: DisplayOptions = {
  defaults: DEFAULT_DISPLAY_SETTINGS,
  timezones: ['UTC', 'Europe/London', 'America/New_York', 'Pacific/Auckland'],
  locales: [
    { value: 'en-GB', label: 'English (United Kingdom)' },
    { value: 'en-US', label: 'English (United States)' },
  ],
  unit_systems: [
    { value: 'metric', label: 'Metric (kg)' },
    { value: 'imperial', label: 'Imperial (lb)' },
  ],
  motion: [
    { value: 'system', label: 'Follow my device setting' },
    { value: 'reduce', label: 'Always reduce motion' },
  ],
  contrast: [
    { value: 'system', label: 'Follow my device setting' },
    { value: 'more', label: 'Always increase contrast' },
  ],
  text_size: [
    { value: 'default', label: 'Default' },
    { value: 'large', label: 'Large' },
    { value: 'x-large', label: 'Extra large' },
  ],
};

const displayResponse = {
  settings: { ...DEFAULT_DISPLAY_SETTINGS, timezone: 'Europe/London' },
  options: displayOptions,
};

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
  retention_options: retentionOptions,
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

/**
 * Actions that can change readiness refresh the account status afterwards. That
 * refresh is a side effect these suites do not assert on, so settle it.
 */
function settleReadiness(http: HttpTestingController): void {
  http.match('/api/setup/state').forEach((req) => req.flush({ account: null, surfaces: {} }));
}

describe('Settings AI provider parity', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Settings],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    // These suites do not exercise the display section; settle its load.
    http.match('/api/settings/display').forEach((req) => req.flush(displayResponse));
    settleReadiness(http);
    http.verify();
  });

  async function loadSettings(
    response: SettingsResponse = settingsResponse,
  ): Promise<ComponentFixture<Settings>> {
    const fixture = TestBed.createComponent(Settings);
    fixture.detectChanges();
    http.expectOne('/api/settings').flush(response);
    http.expectOne('/api/settings/display').flush(displayResponse);
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

  it('renders explicit retain, purge and revoke-only choices for a connected provider', async () => {
    const fixture = TestBed.createComponent(Settings);
    fixture.detectChanges();
    http.expectOne('/api/settings').flush({
      ...settingsResponse,
      gh_configured: true,
      gh_connected: true,
    });
    await fixture.whenStable();
    fixture.detectChanges();
    const labels = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('.retention-option strong'),
    ).map((node) => node.textContent?.trim());
    expect(labels).toEqual([
      'Revoke access only',
      'Disconnect and retain imported data',
      'Disconnect and purge imported data',
    ]);
  });

  it('purges a connector through the tenant-scoped DELETE route when chosen', async () => {
    const connector = {
      provider: 'garmin',
      name: 'Garmin',
      state: 'connected',
      message: null,
      connection_id: 'conn-1',
      retention_options: retentionOptions,
    };
    const response = { ...settingsResponse, connectors_info: [connector] };
    const fixture = TestBed.createComponent(Settings);
    fixture.detectChanges();
    http.expectOne('/api/settings').flush(response);
    await fixture.whenStable();

    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    fixture.componentInstance.setConnectorRetention('garmin', 'purge');
    fixture.componentInstance.disconnectConnector(connector);

    const request = http.expectOne(
      (candidate) => candidate.url === '/api/health/connections/conn-1',
    );
    expect(request.request.method).toBe('DELETE');
    expect(request.request.params.get('mode')).toBe('purge');
    expect(request.request.params.get('confirmed')).toBe('true');
    request.flush('');
    http.expectOne('/api/settings').flush(response);
    confirmSpy.mockRestore();
    expect(fixture.componentInstance.healthStatus()).toContain('purged');
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

    // The server recorded the rejection against the stored key, so the page
    // re-reads it instead of leaving a stale "Connected" badge on screen.
    http.expectOne('/api/settings').flush({
      ...connectedHevyResponse,
      hevy_connection: { ...connectedHevyResponse.hevy_connection, state: 'invalid' },
    });
    await fixture.whenStable();
    fixture.detectChanges();
    expect(component.hevyConnectionLabel()).toBe('Needs attention');
    expect(
      (fixture.nativeElement as HTMLElement).querySelector('#settings-hevy .status.on'),
    ).toBeNull();
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
    http.match('/api/settings/display').forEach((req) => req.flush(displayResponse));
    settleReadiness(http);
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

describe('Settings region and accessibility', () => {
  let http: HttpTestingController;
  const root = document.documentElement;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Settings],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    settleReadiness(http);
    http.verify();
    root.removeAttribute('lang');
    delete root.dataset['motion'];
    delete root.dataset['contrast'];
    delete root.dataset['textSize'];
  });

  async function open(): Promise<ComponentFixture<Settings>> {
    const fixture = TestBed.createComponent(Settings);
    fixture.detectChanges();
    http.expectOne('/api/settings').flush(settingsResponse);
    http.expectOne('/api/settings/display').flush(displayResponse);
    await fixture.whenStable();
    fixture.componentInstance.section.set('display');
    fixture.detectChanges();
    return fixture;
  }

  const control = <T extends HTMLElement>(fixture: ComponentFixture<Settings>, id: string): T =>
    (fixture.nativeElement as HTMLElement).querySelector<T>(`#${id}`)!;

  it('is reachable from the section navigation and shows the saved values', async () => {
    const fixture = await open();
    const element = fixture.nativeElement as HTMLElement;
    const sections = Array.from(element.querySelectorAll('.settings-sections button')).map((b) =>
      b.textContent?.trim(),
    );

    expect(sections).toContain('Region & accessibility');
    expect(control<HTMLInputElement>(fixture, 'display-timezone').value).toBe('Europe/London');
    expect(control<HTMLSelectElement>(fixture, 'display-locale').value).toBe('en-GB');
    expect(
      element.querySelector<HTMLInputElement>('input[name="display-unit-system"]:checked')?.value,
    ).toBe('metric');
    expect(element.querySelectorAll('#display-timezones option').length).toBe(4);
  });

  it('labels every control for assistive technology', async () => {
    const fixture = await open();
    const element = fixture.nativeElement as HTMLElement;

    for (const id of [
      'display-timezone',
      'display-locale',
      'display-motion',
      'display-contrast',
      'display-text-size',
    ]) {
      expect(element.querySelector(`label[for="${id}"]`), id).toBeTruthy();
    }
    expect(element.querySelector('fieldset legend')?.textContent).toContain('Units');
    expect(control(fixture, 'display-status').getAttribute('role')).toBe('status');
  });

  it('saves only the fields that changed and applies them immediately', async () => {
    const fixture = await open();
    const component = fixture.componentInstance;
    component.setDisplayField('unit_system', 'imperial');
    component.setDisplayField('text_size', 'large');
    component.setDisplayField('motion', 'reduce');

    const saving = component.saveDisplay();
    const request = http.expectOne('/api/settings/display');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({
      unit_system: 'imperial',
      motion: 'reduce',
      text_size: 'large',
    });
    request.flush({
      status: 'ok',
      settings: {
        ...displayResponse.settings,
        unit_system: 'imperial',
        motion: 'reduce',
        text_size: 'large',
      },
      changed: ['unit_system', 'motion', 'text_size'],
      regrouped_days: 0,
    });
    await saving;
    fixture.detectChanges();

    expect(component.displayStatus()).toBe('Saved.');
    expect(control(fixture, 'display-status').getAttribute('role')).toBe('status');
    expect(root.dataset['textSize']).toBe('large');
    expect(root.dataset['motion']).toBe('reduce');
  });

  it('explains how many workout days a time-zone change regrouped', async () => {
    const fixture = await open();
    const component = fixture.componentInstance;
    component.setDisplayField('timezone', 'Pacific/Auckland');

    const saving = component.saveDisplay();
    http.expectOne('/api/settings/display').flush({
      status: 'ok',
      settings: { ...displayResponse.settings, timezone: 'Pacific/Auckland' },
      changed: ['timezone'],
      regrouped_days: 2,
    });
    await saving;

    expect(component.displayStatus()).toContain('2 workout days were regrouped');
    expect(component.displayStatus()).toContain('recorded times are unchanged');
  });

  it('does not call the server when nothing changed', async () => {
    const fixture = await open();

    await fixture.componentInstance.saveDisplay();

    expect(fixture.componentInstance.displayStatus()).toBe('No changes to save.');
  });

  it("shows the server's reason when a value is rejected", async () => {
    const fixture = await open();
    const component = fixture.componentInstance;
    component.setDisplayField('timezone', 'Mars/Base');

    const saving = component.saveDisplay();
    http
      .expectOne('/api/settings/display')
      .flush(
        { detail: 'Unsupported time zone. Use an IANA name such as Europe/London.' },
        { status: 400, statusText: 'Bad Request' },
      );
    await saving;
    fixture.detectChanges();

    expect(component.displayStatus()).toContain('Unsupported time zone');
    expect(control(fixture, 'display-error').getAttribute('role')).toBe('alert');
    expect(control(fixture, 'display-error').textContent).toContain('Unsupported time zone');
    expect(control(fixture, 'display-status').textContent?.trim()).toBe('');
    expect(component.displaySaving()).toBe(false);
  });

  it('ignores a second save while one is in flight', async () => {
    const fixture = await open();
    const component = fixture.componentInstance;
    component.setDisplayField('contrast', 'more');

    const first = component.saveDisplay();
    const second = component.saveDisplay();
    http.expectOne('/api/settings/display').flush({
      status: 'ok',
      settings: { ...displayResponse.settings, contrast: 'more' },
      changed: ['contrast'],
      regrouped_days: 0,
    });
    await Promise.all([first, second]);
  });

  it("offers the device's time zone only when the server supports it", async () => {
    const fixture = await open();
    const component = fixture.componentInstance;
    const resolved = vi.spyOn(Intl.DateTimeFormat.prototype, 'resolvedOptions');

    resolved.mockReturnValue({
      timeZone: 'America/New_York',
    } as Intl.ResolvedDateTimeFormatOptions);
    component.useDeviceTimeZone();
    expect(component.displayDraft().timezone).toBe('America/New_York');
    expect(component.displayStatusIsError()).toBe(false);

    resolved.mockReturnValue({
      timeZone: 'Antarctica/Troll',
    } as Intl.ResolvedDateTimeFormatOptions);
    component.useDeviceTimeZone();
    expect(component.displayDraft().timezone).toBe('America/New_York');
    expect(component.displayStatus()).toContain('does not support');
    expect(component.displayStatusIsError()).toBe(true);
    resolved.mockRestore();
  });
});


describe('Settings readiness', () => {
  let http: HttpTestingController;
  let setupState: SetupStateService;

  const readyAccount: SetupAccount = {
    state: 'ready',
    label: 'Your training data is up to date',
    reason: '',
    reason_code: null,
    first_run: false,
    checks: { hevy_connected: true },
    missing: [],
    last_activity_at: null,
    age_days: null,
    stale: false,
    connection_state: 'connected',
    connector: {
      provider: 'hevy',
      operation: null,
      operation_started_at: null,
      operation_stalled: false,
      last_sync: {
        outcome: 'succeeded',
        at: '2026-09-02T10:00:00+00:00',
        code: null,
        records: 40,
      },
    },
    ai_provider_state: 'verified',
  };

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Settings],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
    setupState = TestBed.inject(SetupStateService);
  });

  afterEach(() => {
    http.match('/api/settings/display').forEach((req) => req.flush(displayResponse));
    settleReadiness(http);
    http.verify();
  });

  async function open(
    response: SettingsResponse = connectedHevyResponse,
  ): Promise<ComponentFixture<Settings>> {
    const fixture = TestBed.createComponent(Settings);
    fixture.detectChanges();
    http.expectOne('/api/settings').flush(response);
    http.expectOne('/api/settings/display').flush(displayResponse);
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  function text(fixture: ComponentFixture<Settings>, selector: string): string {
    return (
      (fixture.nativeElement as HTMLElement).querySelector(selector)?.textContent?.trim() ?? ''
    );
  }

  it('shows nothing about readiness until the server has said what it is', async () => {
    const fixture = await open();

    expect((fixture.nativeElement as HTMLElement).querySelector('#settings-readiness')).toBeNull();
  });

  it('says it could not check, rather than implying all is well, when readiness fails to load', async () => {
    setupState.failed.set(true);
    const fixture = await open();

    expect(text(fixture, '#settings-readiness-failed')).toContain('nothing here is confirmed');
    expect(
      (fixture.nativeElement as HTMLElement).querySelector('#settings-readiness-failed')?.getAttribute('role'),
    ).toBe('alert');
  });

  it('shows the server-derived account status with Hevy, last sync and AI facts', async () => {
    setupState.account.set(readyAccount);
    const fixture = await open();

    expect(text(fixture, '#settings-readiness .status')).toContain('up to date');
    expect(text(fixture, '#readiness-hevy')).toContain('Connected');
    expect(text(fixture, '#readiness-sync')).toContain('Completed on 2026-09-02');
    expect(text(fixture, '#readiness-sync')).toContain('40 workouts found');
    expect(text(fixture, '#readiness-ai')).toContain('Verified');
    expect((fixture.nativeElement as HTMLElement).querySelector('#readiness-reason')).toBeNull();
  });

  it('raises an alert for a degraded account and names what failed', async () => {
    setupState.account.set({
      ...readyAccount,
      state: 'degraded',
      label: 'Your Hevy connection needs attention',
      reason: 'Hevy rejected your stored API key, so nothing new can be imported.',
      reason_code: 'credential_rejected',
      connection_state: 'invalid',
      ai_provider_state: 'failed',
    });
    const fixture = await open();
    const panel = (fixture.nativeElement as HTMLElement).querySelector('#settings-readiness');

    expect(panel?.getAttribute('data-state')).toBe('degraded');
    expect(text(fixture, '#readiness-reason')).toContain('rejected');
    expect(
      (fixture.nativeElement as HTMLElement).querySelector('#readiness-reason')?.getAttribute('role'),
    ).toBe('alert');
    expect(text(fixture, '#readiness-hevy')).toContain('rejected');
    expect(text(fixture, '#readiness-ai')).toContain('failed');
    expect(text(fixture, '#settings-readiness .status')).not.toContain('up to date');
  });

  it('disables the sync button while any session is syncing and refuses to start another', async () => {
    setupState.account.set({
      ...readyAccount,
      state: 'syncing',
      connector: { ...readyAccount.connector!, operation: 'syncing' },
    });
    const fixture = await open();
    const button = (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>(
      '#hevy-sync-btn',
    );
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    fixture.componentInstance.syncHistory();

    expect(button?.disabled).toBe(true);
    expect(button?.textContent).toContain('Sync running');
    http.expectNone('/api/settings/sync-history');
  });

  it('says syncing in the status panel while its own sync runs, then refreshes readiness', async () => {
    setupState.account.set(readyAccount);
    const fixture = await open();
    const component = fixture.componentInstance;
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    expect(text(fixture, '#settings-readiness .status')).toContain('up to date');

    component.syncHistory();
    fixture.detectChanges();

    expect(component.syncing()).toBe(true);
    expect(component.connectorBusy()).toBe(true);
    // The panel never shows the pre-sync snapshot as if it were current.
    expect(text(fixture, '#settings-readiness .status')).toContain('Syncing your Hevy history');
    expect(text(fixture, '#readiness-reason')).toContain('sync is running');
    expect(text(fixture, '#readiness-hevy')).toContain('Syncing');
    expect(
      (fixture.nativeElement as HTMLElement).querySelector('#settings-readiness')?.getAttribute(
        'data-state',
      ),
    ).toBe('syncing');
    http.expectNone('/api/setup/state');

    http.expectOne('/api/settings/sync-history').flush({ processed: 5, workouts_found: 5 });
    fixture.detectChanges();

    expect(component.syncing()).toBe(false);
    expect(component.hevyStatus()).toContain('Synced! 5 of 5');
    http.expectOne('/api/setup/state').flush({ account: readyAccount, surfaces: {} });
    fixture.detectChanges();
    expect(text(fixture, '#settings-readiness .status')).toContain('up to date');
  });

  it('shows the server reason when a sync is refused or fails, and frees the button', async () => {
    const fixture = await open();
    const component = fixture.componentInstance;
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    component.syncHistory();
    http
      .expectOne('/api/settings/sync-history')
      .flush(
        { detail: 'A Hevy sync is already running for this account. Try again when it finishes.' },
        { status: 409, statusText: 'Conflict' },
      );

    expect(component.hevyStatus()).toContain('already running');
    expect(component.syncing()).toBe(false);
    expect(component.hevyStatus()).not.toContain('Synced');
  });

  it('labels a stored AI key by its last connection test, not by its presence', async () => {
    const withTest = (status: string | null, at: string | null): SettingsResponse => ({
      ...settingsResponse,
      user_keys: {
        ...settingsResponse.user_keys,
        claude: {
          has_key: true,
          masked: '••••••••key1',
          model: 'claude-stored',
          last_test_status: status,
          last_tested_at: at,
        },
      },
    });

    const untested = await open(withTest(null, null));
    expect(text(untested, '#ai-key-state')).toContain('Not tested');
    expect(untested.nativeElement.querySelector('#ai-key-state').classList).not.toContain('on');
    expect(text(untested, '#claude-key-detail')).toContain('never tested');
    untested.destroy();

    const verified = TestBed.createComponent(Settings);
    verified.detectChanges();
    http.expectOne('/api/settings').flush(withTest('ok', '2026-09-03T00:00:00+00:00'));
    http.expectOne('/api/settings/display').flush(displayResponse);
    await verified.whenStable();
    verified.detectChanges();
    expect(text(verified, '#ai-key-state')).toContain('Verified');
    expect(verified.nativeElement.querySelector('#ai-key-state').classList).toContain('on');
    expect(text(verified, '#claude-key-detail')).toContain('on 2026-09-03');
    verified.destroy();

    const failed = TestBed.createComponent(Settings);
    failed.detectChanges();
    http.expectOne('/api/settings').flush(withTest('rejected', '2026-09-03T00:00:00+00:00'));
    http.expectOne('/api/settings/display').flush(displayResponse);
    await failed.whenStable();
    failed.detectChanges();
    expect(text(failed, '#ai-key-state')).toContain('Test failed');
    expect(failed.nativeElement.querySelector('#ai-key-state').classList).not.toContain('on');
  });

  it('reloads what the server recorded and refreshes readiness after a key test', async () => {
    const fixture = await open();
    fixture.componentInstance.testKey('claude');

    http.expectOne('/api/settings/key/test').flush({ status: 'ok', provider: 'claude' });
    http.expectOne('/api/settings').flush(settingsResponse);
    http.expectOne('/api/setup/state').flush({ account: readyAccount, surfaces: {} });
    await fixture.whenStable();

    expect(setupState.account()?.ai_provider_state).toBe('verified');
  });
});
