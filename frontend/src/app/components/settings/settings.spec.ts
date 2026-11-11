import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Settings } from './settings';

const providers = [
  { id: 'gemini', name: 'Google Gemini', default_model: 'gemini-3.8-flash' },
  { id: 'claude', name: 'Anthropic Claude', default_model: 'claude-sonnet-5-5' },
  { id: 'openai', name: 'OpenAI', default_model: 'gpt-4o' },
  { id: 'deepseek', name: 'DeepSeek', default_model: 'deepseek-flash' },
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
      'deepseek-flash',
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
  describe('AI connection tests', () => {
    it('tests a typed key and model without saving anything', async () => {
      const fixture = await loadSettings();
      const component = fixture.componentInstance;
      component.aiKeyInputs['openai'] = '  synthetic-openai-key  ';
      component.aiModelInputs['openai'] = 'gpt-synthetic';

      component.testConnection('openai');

      const request = http.expectOne('/api/settings/ai/test');
      expect(request.request.method).toBe('POST');
      expect(request.request.body).toEqual({
        provider: 'openai',
        api_key: 'synthetic-openai-key',
        model: 'gpt-synthetic',
      });
      expect(component.isAiBusy('openai', 'test')).toBe(true);
      expect(component.aiStatus()).toBe('Testing connection…');
      request.flush({ status: 'ok', code: 'ok', message: 'Connected: OpenAI accepted this key.' });

      expect(component.aiBusy()).toBeNull();
      expect(component.aiStatus()).toBe('✓ Connected: OpenAI accepted this key.');
      expect(component.aiStatusKind()).toBe('success');
      // Testing is read-only: the typed key stays put and nothing is saved.
      expect(component.aiKeyInputs['openai']).toBe('  synthetic-openai-key  ');
      http.expectNone('/api/settings/key');
    });

    it('tests the stored key when none is typed, without sending any key', async () => {
      const fixture = await loadSettings();
      const component = fixture.componentInstance;

      component.testConnection('claude');

      const request = http.expectOne('/api/settings/ai/test');
      expect(request.request.body).toEqual({ provider: 'claude', model: 'claude-selected' });
      expect('api_key' in request.request.body).toBe(false);
      request.flush({ status: 'ok', message: 'Connected.' });
    });

    it('explains how to fix a failed test and marks the status as an error', async () => {
      const fixture = await loadSettings();
      const component = fixture.componentInstance;
      component.aiKeyInputs['openai'] = 'synthetic-openai-key';

      component.testConnection('openai');
      http.expectOne('/api/settings/ai/test').flush({
        status: 'error',
        code: 'invalid_credentials',
        message: 'OpenAI rejected the API key. Check that you pasted the whole key.',
      });
      fixture.detectChanges();

      expect(component.aiStatus()).toBe(
        '✗ OpenAI rejected the API key. Check that you pasted the whole key.',
      );
      const status = (fixture.nativeElement as HTMLElement).querySelector('#ai-status');
      expect(status?.classList.contains('error')).toBe(true);
      expect(status?.textContent).toContain('rejected the API key');
    });

    it('shows the throttle message when the server rate limits tests', async () => {
      const fixture = await loadSettings();
      const component = fixture.componentInstance;
      component.aiKeyInputs['openai'] = 'synthetic-openai-key';

      component.testConnection('openai');
      http
        .expectOne('/api/settings/ai/test')
        .flush(
          { detail: 'Too many connection tests. Wait 42 seconds and try again.' },
          { status: 429, statusText: 'Too Many Requests' },
        );

      expect(component.aiBusy()).toBeNull();
      expect(component.aiStatus()).toContain('Wait 42 seconds');
      expect(component.aiStatusKind()).toBe('error');
    });

    it('does not call the API when there is no key to test', async () => {
      const fixture = await loadSettings();
      const component = fixture.componentInstance;

      expect(component.canTestAiKey('openai')).toBe(false);
      component.testConnection('openai');

      http.expectNone('/api/settings/ai/test');
      expect(component.aiStatus()).toBe('Enter an API key to test.');
    });

    it('ignores a second request while one is in flight', async () => {
      const fixture = await loadSettings();
      const component = fixture.componentInstance;
      component.aiKeyInputs['openai'] = 'synthetic-openai-key';

      component.testConnection('openai');
      component.testConnection('openai');
      component.saveKey('openai');

      http.expectOne('/api/settings/ai/test').flush({ status: 'ok', message: 'Connected.' });
    });

    it('renders an accessible Test button that is disabled until there is a key', async () => {
      const fixture = await loadSettings();
      const element = fixture.nativeElement as HTMLElement;
      const openaiButton = element.querySelector<HTMLButtonElement>('#openai-test-btn');
      const claudeButton = element.querySelector<HTMLButtonElement>('#claude-test-btn');

      expect(openaiButton?.getAttribute('aria-label')).toBe('Test OpenAI connection');
      expect(openaiButton?.disabled).toBe(true);
      expect(claudeButton?.disabled).toBe(false);

      // Type like a user so the form binding (and change detection) runs.
      const input = element.querySelector<HTMLInputElement>('#openai-key-input');
      input!.value = 'synthetic-openai-key';
      input!.dispatchEvent(new Event('input'));
      fixture.detectChanges();
      expect(openaiButton?.disabled).toBe(false);
    });

    it('verifies an AI key on save and clears it only once it was accepted', async () => {
      const fixture = await loadSettings();
      const component = fixture.componentInstance;
      component.aiKeyInputs['openai'] = 'synthetic-openai-key';
      component.aiModelInputs['openai'] = 'gpt-synthetic';

      component.saveKey('openai');

      expect(component.isAiBusy('openai', 'save')).toBe(true);
      expect(component.aiStatus()).toBe('Verifying key with the provider…');
      const request = http.expectOne('/api/settings/key');
      expect(request.request.body).toEqual({
        provider: 'openai',
        api_key: 'synthetic-openai-key',
        model: 'gpt-synthetic',
      });
      request.flush({ status: 'ok', provider: 'openai' });
      http.expectOne('/api/settings').flush(settingsResponse);

      expect(component.aiKeyInputs['openai']).toBe('');
      expect(component.aiStatus()).toBe('✓ openai key verified and saved.');
      expect(component.aiBusy()).toBeNull();
    });

    it('keeps the typed key and shows why when the provider refuses it', async () => {
      const fixture = await loadSettings();
      const component = fixture.componentInstance;
      component.aiKeyInputs['openai'] = 'synthetic-openai-key';

      component.saveKey('openai');
      http.expectOne('/api/settings/key').flush(
        {
          status: 'error',
          code: 'invalid_credentials',
          detail: 'OpenAI rejected the API key. Your key was not saved.',
        },
        { status: 422, statusText: 'Unprocessable Entity' },
      );

      expect(component.aiKeyInputs['openai']).toBe('synthetic-openai-key');
      expect(component.aiStatus()).toBe('✗ OpenAI rejected the API key. Your key was not saved.');
      expect(component.aiStatusKind()).toBe('error');
      expect(component.aiBusy()).toBeNull();
      http.expectNone('/api/settings');
    });

    it('shows the refusal when activating a model the provider cannot serve', async () => {
      const fixture = await loadSettings();
      const component = fixture.componentInstance;
      component.selectProvider('claude');
      component.aiModelInputs['claude'] = 'claude-typo-9';

      component.savePreferences();
      http
        .expectOne('/api/settings/preferences')
        .flush(
          { detail: "Anthropic Claude does not offer a model named 'claude-typo-9' to this key." },
          { status: 422, statusText: 'Unprocessable Entity' },
        );

      expect(component.prefStatus()).toContain('does not offer a model');
    });
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
