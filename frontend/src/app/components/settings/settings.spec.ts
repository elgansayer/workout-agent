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
  ai_providers: providers,
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
