import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { vi } from 'vitest';
import { Settings } from './settings';

const providers = [
  { id: 'gemini', name: 'Google Gemini', default_model: 'gemini-3.8-flash' },
  { id: 'claude', name: 'Anthropic Claude', default_model: 'claude-sonnet-4-20250514' },
  { id: 'openai', name: 'OpenAI', default_model: 'gpt-4o' },
  { id: 'deepseek', name: 'DeepSeek', default_model: 'deepseek-chat' },
];

const settingsResponse = {
  user_prefs: {
    preferred_ai: 'claude',
    ai_model: 'claude-selected',
    goals: ['strength'],
    experience_level: 'advanced',
    constraints: [],
  },
  user_keys: {
    hevy: { has_key: false, masked: null, model: null },
    gemini: { has_key: false, masked: null, model: null },
    claude: { has_key: true, masked: '••••••••key1', model: 'claude-stored' },
    openai: { has_key: false, masked: null, model: null },
    deepseek: { has_key: false, masked: null, model: null },
  },
  ai_providers: providers,
  gh_connected: false,
  gh_configured: false,
  connectors_info: [],
  vapid_public_key: null,
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

  async function loadSettings(): Promise<ComponentFixture<Settings>> {
    const fixture = TestBed.createComponent(Settings);
    fixture.detectChanges();
    http.expectOne('/api/settings').flush(settingsResponse);
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
    // Switching provider changes which credential the AI badge describes.
    http.expectOne('/api/settings').flush(settingsResponse);
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
});

const component = (
  connector: string,
  label: string,
  state: string,
  reason: string,
  message: string,
  extra: Record<string, unknown> = {},
) => ({
  connector,
  label,
  state,
  reason,
  message,
  next_action: 'none',
  action_label: null,
  action_href: null,
  retryable: false,
  freshness: null,
  last_attempt_at: null,
  last_success_at: null,
  last_error_code: null,
  consecutive_failures: 0,
  last_run: null,
  ...extra,
});

const integrations = {
  generated_at: '2026-11-11T12:00:00+00:00',
  state: 'degraded',
  reason: 'credential_rejected',
  message: 'Hevy rejected your credentials. Update them to continue.',
  next_action: 'reconnect',
  action_label: 'Update Hevy credentials',
  action_href: '/settings?section=connections',
  required: ['hevy', 'programme', 'ai_provider'],
  blocking: ['hevy'],
  components: {
    hevy: component(
      'hevy',
      'Hevy',
      'degraded',
      'credential_rejected',
      'Hevy rejected your credentials. Update them to continue.',
    ),
    programme: component(
      'programme',
      'Programme',
      'ready',
      'programme_active',
      'Your programme is active.',
    ),
    ai_provider: component(
      'ai:claude',
      'AI coach (Anthropic Claude)',
      'ready',
      'configured',
      'AI coach (Anthropic Claude) is configured.',
    ),
    google_health: component(
      'google_health',
      'Google Health',
      'setup_required',
      'not_available',
      'Google Health OAuth credentials are not set on the server.',
    ),
  },
};

describe('Settings shared integration state', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Settings],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    http.verify();
  });

  async function load(
    response: object = { ...settingsResponse, integrations },
  ): Promise<ComponentFixture<Settings>> {
    const fixture = TestBed.createComponent(Settings);
    fixture.detectChanges();
    http.expectOne('/api/settings').flush(response);
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  it('renders connector readiness from the model, not from key presence', async () => {
    const fixture = await load();
    const el = fixture.nativeElement as HTMLElement;
    const badge = (selector: string) =>
      el.querySelector(`${selector} .card-head .state-badge`)?.textContent?.trim();

    // The Hevy key exists, yet the connector is degraded because the key was rejected.
    expect(badge('#settings-hevy')).toBe('Degraded');
    expect(el.querySelector('#settings-hevy [role="alert"]')?.textContent).toContain(
      'rejected your credentials',
    );
    expect(badge('#settings-ai')).toBe('Ready');
    expect(el.querySelector('#settings-connectors .state-badge')?.textContent?.trim()).toBe(
      'Setup required',
    );
    expect(el.querySelector('#settings-connectors')?.textContent).toContain(
      'OAuth credentials are not set on the server',
    );
    expect(el.textContent).not.toContain('Not connected');
  });

  it('degrades gracefully when an older API omits the shared model', async () => {
    const fixture = await load(settingsResponse);
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('#settings-hevy .state-badge')).toBeNull();
    expect(el.querySelector('#settings-hevy .key-form')).not.toBeNull();
  });

  it('uses the registry connector state from the shared vocabulary', async () => {
    const fixture = await load({
      ...settingsResponse,
      integrations,
      connectors_info: [
        { provider: 'garmin', name: 'Garmin', state: 'degraded', message: 'Provider down' },
      ],
    });
    const rows = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('#settings-connectors h3'),
    ).map((h) => h.parentElement?.textContent?.replace(/\s+/g, ' ').trim());
    expect(rows.some((text) => text?.includes('Garmin') && text.includes('Degraded'))).toBe(true);
  });

  it('verifies the stored credential without sending a placeholder key', async () => {
    const fixture = await load();
    const settings = fixture.componentInstance;
    settings.verifyHevy();
    const request = http.expectOne('/api/settings/verify-hevy');
    expect(request.request.body).toEqual({});
    request.flush({ status: 'ok', workout_count: 7, recorded: true });
    expect(settings.hevyStatus()).toContain('7 workouts');
    http.expectOne('/api/settings').flush({ ...settingsResponse, integrations });
  });

  it('verifies a typed candidate key as typed and shows the server explanation', async () => {
    const fixture = await load();
    const settings = fixture.componentInstance;
    settings.hevyKeyInput = ' typed-synthetic-key ';
    settings.verifyHevy();
    const request = http.expectOne('/api/settings/verify-hevy');
    expect(request.request.body).toEqual({ api_key: 'typed-synthetic-key' });
    request.flush({ status: 'error', detail: 'Hevy rejected your credentials.' });
    expect(settings.hevyStatus()).toBe('Hevy rejected your credentials.');
    http.expectOne('/api/settings').flush({ ...settingsResponse, integrations });
  });

  it('reports a partial import and refreshes readiness', async () => {
    vi.stubGlobal('confirm', () => true);
    const fixture = await load();
    const settings = fixture.componentInstance;
    settings.syncHistory();
    http
      .expectOne('/api/settings/sync-history')
      .flush({ status: 'partial', workouts_found: 10, processed: 6, failed: 3 });
    expect(settings.hevyStatus()).toContain('Partly synced: 6 of 10');
    http.expectOne('/api/settings').flush({ ...settingsResponse, integrations });
  });

  it('shows why a sync was refused and still refreshes readiness', async () => {
    vi.stubGlobal('confirm', () => true);
    const fixture = await load();
    const settings = fixture.componentInstance;
    settings.syncHistory();
    http.expectOne('/api/settings/sync-history').flush(
      { detail: 'A Hevy sync is already running. Wait for it to finish.' },
      {
        status: 409,
        statusText: 'Conflict',
      },
    );
    expect(settings.hevyStatus()).toContain('already running');
    http.expectOne('/api/settings').flush({ ...settingsResponse, integrations });
  });

  it('does nothing when the user declines the destructive rebuild', async () => {
    vi.stubGlobal('confirm', () => false);
    const fixture = await load();
    fixture.componentInstance.syncHistory();
    http.expectNone('/api/settings/sync-history');
  });
});

describe('Settings deep links', () => {
  async function open(section: string | null) {
    await TestBed.configureTestingModule({
      imports: [Settings],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { snapshot: { queryParamMap: convertToParamMap(section ? { section } : {}) } },
        },
      ],
    }).compileComponents();
    const fixture = TestBed.createComponent(Settings);
    fixture.detectChanges();
    TestBed.inject(HttpTestingController).expectOne('/api/settings').flush(settingsResponse);
    await fixture.whenStable();
    return fixture.componentInstance;
  }

  it('opens the section a banner links to', async () => {
    expect((await open('ai')).section()).toBe('ai');
  });

  it('ignores unknown sections and defaults to connections', async () => {
    expect((await open('../../etc/passwd')).section()).toBe('connections');
  });
});
