import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
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

const exportId = '0f3c6d1e9a7b4c2d8e5f1a2b3c4d5e6f';

function exportItem(overrides: Record<string, unknown> = {}) {
  return {
    id: exportId,
    status: 'ready',
    requested_at: '2026-11-10T12:00:00+00:00',
    completed_at: '2026-11-10T12:00:03+00:00',
    expires_at: '2026-11-11T12:00:03+00:00',
    size_bytes: 11566,
    checksum_sha256: 'abcdef0123456789'.repeat(4),
    error_code: null,
    download_url: `/api/account/exports/${exportId}/download`,
    ...overrides,
  };
}

function exportList(exports: unknown[], overrides: Record<string, unknown> = {}) {
  return { exports, retention_hours: 24, enabled: true, ...overrides };
}

describe('Settings account data export', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Settings],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    http.verify();
  });

  async function openDataSection(
    list: object = exportList([]),
  ): Promise<ComponentFixture<Settings>> {
    // HttpClient testing flushes synchronously, so these tests render with
    // detectChanges() and never wait on whenStable(), which fake timers would stall.
    const fixture = TestBed.createComponent(Settings);
    fixture.detectChanges();
    http.expectOne('/api/settings').flush(settingsResponse);
    fixture.detectChanges();
    const dataTab = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll<HTMLButtonElement>(
        '.settings-sections button',
      ),
    ).find((button) => button.textContent?.trim() === 'Your data');
    expect(dataTab).toBeTruthy();
    dataTab!.click();
    http.expectOne('/api/account/exports').flush(list);
    fixture.detectChanges();
    return fixture;
  }

  it('does not request exports until the Your data section is opened', async () => {
    const fixture = TestBed.createComponent(Settings);
    fixture.detectChanges();
    http.expectOne('/api/settings').flush(settingsResponse);
    await fixture.whenStable();
    fixture.detectChanges();
    http.expectNone('/api/account/exports');
    const section = (fixture.nativeElement as HTMLElement).querySelector<HTMLElement>(
      '#settings-data',
    );
    expect(section?.hidden).toBe(true);
  });

  it('explains what is included, what is never included, and how long exports last', async () => {
    const fixture = await openDataSection(exportList([], { retention_hours: 6 }));
    const element = fixture.nativeElement as HTMLElement;
    const section = element.querySelector<HTMLElement>('#settings-data')!;
    expect(section.hidden).toBe(false);
    const text = section.textContent?.replace(/\s+/g, ' ') ?? '';
    expect(text).toContain('coaching history');
    expect(text).toContain('audit events');
    expect(text).toContain('API keys, tokens and other secrets are never included');
    expect(text).toContain('deleted after 6 hours');
    expect(element.querySelector('#export-empty')).toBeTruthy();
    expect(element.querySelector<HTMLButtonElement>('#export-request-btn')?.disabled).toBe(false);
  });

  it('requests an export without sending any owner identifier', async () => {
    const fixture = await openDataSection();
    const component = fixture.componentInstance;

    component.requestExport();

    const request = http.expectOne('/api/account/exports');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({});
    request.flush({ export: exportItem({ status: 'queued' }), created: true });
    http
      .expectOne('/api/account/exports')
      .flush(exportList([exportItem({ status: 'queued', download_url: null })]));
    fixture.detectChanges();

    expect(component.exportStatus()).toContain('Export requested');
    const element = fixture.nativeElement as HTMLElement;
    expect(element.querySelector('.export-item')?.textContent).toContain('Queued');
    expect(element.querySelector('.export-item a')).toBeNull();
    // A second request is blocked while one export is in flight.
    expect(element.querySelector<HTMLButtonElement>('#export-request-btn')?.disabled).toBe(true);
    component.requestExport();
    http.expectNone('/api/account/exports');
    fixture.destroy();
  });

  it('polls while an export builds and offers the download once it is ready', async () => {
    vi.useFakeTimers();
    const fixture = await openDataSection(
      exportList([exportItem({ status: 'running', download_url: null })]),
    );
    const element = fixture.nativeElement as HTMLElement;
    expect(element.querySelector('.export-item')?.textContent).toContain('Building');
    expect(element.querySelector('.export-item [aria-busy="true"]')).toBeTruthy();

    await vi.advanceTimersByTimeAsync(2000);
    http.expectOne('/api/account/exports').flush(exportList([exportItem()]));
    fixture.detectChanges();

    const link = element.querySelector<HTMLAnchorElement>('.export-item a[download]');
    expect(link?.getAttribute('href')).toBe(`/api/account/exports/${exportId}/download`);
    expect(link?.getAttribute('aria-label')).toContain('Download the export requested');
    expect(element.querySelector('.export-item button')?.getAttribute('aria-label')).toContain(
      'Delete the export requested',
    );
    expect(element.querySelector('.export-item')?.textContent).toContain('11.3 KB');
    expect(element.querySelector('.export-item code')?.textContent).toContain('abcdef012345');
    // Polling stops once nothing is in flight.
    await vi.advanceTimersByTimeAsync(6000);
    http.expectNone('/api/account/exports');
  });

  it('stops polling when the section is left or the page is closed', async () => {
    vi.useFakeTimers();
    const fixture = await openDataSection(
      exportList([exportItem({ status: 'queued', download_url: null })]),
    );
    fixture.componentInstance.selectSection('profile');
    await vi.advanceTimersByTimeAsync(10000);
    http.expectNone('/api/account/exports');

    fixture.componentInstance.selectSection('data');
    http
      .expectOne('/api/account/exports')
      .flush(exportList([exportItem({ status: 'queued', download_url: null })]));
    fixture.destroy();
    await vi.advanceTimersByTimeAsync(10000);
    http.expectNone('/api/account/exports');
  });

  it('shows a safe message for a failed export and never offers a download', async () => {
    const fixture = await openDataSection(
      exportList([
        exportItem({ status: 'failed', error_code: 'export_timeout', download_url: null }),
      ]),
    );
    const element = fixture.nativeElement as HTMLElement;
    const item = element.querySelector('.export-item')!;
    expect(item.textContent).toContain('Failed');
    expect(item.textContent).toContain('took too long');
    expect(item.querySelector('a')).toBeNull();
  });

  it('shows expired exports as gone without a delete action', async () => {
    const fixture = await openDataSection(
      exportList([exportItem({ status: 'expired', download_url: null })]),
    );
    const item = (fixture.nativeElement as HTMLElement).querySelector('.export-item')!;
    expect(item.textContent).toContain('expired');
    expect(item.querySelector('a')).toBeNull();
    expect(item.querySelector('button')).toBeNull();
  });

  it('deletes an export after confirmation and refreshes the list', async () => {
    const fixture = await openDataSection(exportList([exportItem()]));
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(true));

    fixture.componentInstance.deleteExport(exportItem() as never);

    const request = http.expectOne(`/api/account/exports/${exportId}`);
    expect(request.request.method).toBe('DELETE');
    request.flush({ status: 'ok' });
    http.expectOne('/api/account/exports').flush(exportList([]));
    expect(fixture.componentInstance.exportStatus()).toBe('Export deleted.');
    expect(fixture.componentInstance.exports()).toEqual([]);
  });

  it('does not delete anything when the confirmation is declined', async () => {
    const fixture = await openDataSection(exportList([exportItem()]));
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(false));
    fixture.componentInstance.deleteExport(exportItem() as never);
    http.expectNone(`/api/account/exports/${exportId}`);
  });

  it('disables requests and explains when the operator has turned exports off', async () => {
    const fixture = await openDataSection(exportList([], { enabled: false }));
    const element = fixture.nativeElement as HTMLElement;
    expect(element.querySelector('#export-disabled')).toBeTruthy();
    expect(element.querySelector<HTMLButtonElement>('#export-request-btn')?.disabled).toBe(true);
    fixture.componentInstance.requestExport();
    http.expectNone('/api/account/exports');
  });

  it('turns request failures into plain-language messages', async () => {
    const fixture = await openDataSection();
    const component = fixture.componentInstance;
    const cases: Array<[number, string]> = [
      [429, 'export limit'],
      [503, 'temporarily unavailable'],
      [401, 'Sign in again'],
    ];
    for (const [status, expected] of cases) {
      component.requestExport();
      http
        .expectOne('/api/account/exports')
        .flush({ detail: 'ignored' }, { status, statusText: 'error' });
      expect(component.exportStatus()).toContain(expected);
      expect(component.exportBusy()).toBe(false);
    }
  });

  it('reports a failure to load the export list', async () => {
    const fixture = TestBed.createComponent(Settings);
    fixture.detectChanges();
    http.expectOne('/api/settings').flush(settingsResponse);
    fixture.componentInstance.selectSection('data');
    http
      .expectOne('/api/account/exports')
      .flush({ detail: 'boom' }, { status: 500, statusText: 'error' });
    expect(fixture.componentInstance.exportStatus()).toBe('Could not load your exports.');
    expect(fixture.componentInstance.exportsLoaded()).toBe(true);
  });

  it('formats sizes for people', () => {
    const settings = TestBed.createComponent(Settings).componentInstance;
    expect(settings.formatBytes(null)).toBe('');
    expect(settings.formatBytes(512)).toBe('512 B');
    expect(settings.formatBytes(2048)).toBe('2.0 KB');
    expect(settings.formatBytes(5 * 1024 * 1024)).toBe('5.0 MB');
  });
});
