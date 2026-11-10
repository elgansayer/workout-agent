import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { vi } from 'vitest';
import { Settings } from './settings';

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
];

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
  retention_options: retentionOptions,
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
});
