import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { DEFAULT_DISPLAY_SETTINGS, DisplayOptions } from '../../services/display-settings';
import { Settings } from './settings';

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
