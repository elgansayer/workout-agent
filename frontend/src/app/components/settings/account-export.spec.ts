import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { vi } from 'vitest';
import { AccountExport, ExportInfo, ExportRecord } from './account-export';

const URL = '/api/account/exports';

const record = (overrides: Partial<ExportRecord> = {}): ExportRecord => ({
  export_id: 'a'.repeat(32),
  state: 'queued',
  requested_at: '2026-11-12T09:00:00+00:00',
  started_at: null,
  completed_at: null,
  expires_at: null,
  size_bytes: null,
  sha256: null,
  record_count: null,
  format_version: 1,
  attempts: 0,
  error_code: null,
  can_download: false,
  download_url: null,
  ...overrides,
});

const ready = (overrides: Partial<ExportRecord> = {}): ExportRecord =>
  record({
    state: 'ready',
    started_at: '2026-11-12T09:00:05+00:00',
    completed_at: '2026-11-12T09:00:09+00:00',
    expires_at: '2026-11-13T09:00:09+00:00',
    size_bytes: 1_572_864,
    sha256: 'f'.repeat(64),
    record_count: 321,
    can_download: true,
    download_url: `${URL}/${'a'.repeat(32)}/download`,
    ...overrides,
  });

const info = (exports: ExportRecord[] = [], overrides: Partial<ExportInfo> = {}): ExportInfo => ({
  eligible: true,
  reason: null,
  retention_hours: 24,
  daily_limit: 5,
  remaining_today: 5,
  format_version: 1,
  included: [
    { category: 'profile', label: 'Profile, preferences and sign-in methods' },
    { category: 'workouts', label: 'Workouts, exercise progress and Hevy routines' },
  ],
  excluded: [
    { category: 'credentials', reason: 'API keys, OAuth and refresh tokens and push keys.' },
    { category: 'other_accounts', reason: 'Records that belong to other accounts.' },
  ],
  exports,
  ...overrides,
});

describe('AccountExport', () => {
  let http: HttpTestingController;
  let polls: { handler: () => void; ms: number }[];

  beforeEach(async () => {
    polls = [];
    const realSetTimeout = globalThis.setTimeout;
    // Capture only the export polls; every other timer (Angular's own) runs normally.
    vi.spyOn(globalThis, 'setTimeout').mockImplementation(((
      handler: TimerHandler,
      ms?: number,
      ...args: unknown[]
    ) => {
      if ((ms === 3000 || ms === 60000) && typeof handler === 'function') {
        polls.push({ handler: handler as () => void, ms });
        return polls.length as unknown as ReturnType<typeof setTimeout>;
      }
      return realSetTimeout(handler, ms, ...args);
    }) as unknown as typeof setTimeout);
    await TestBed.configureTestingModule({
      imports: [AccountExport],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    http.verify();
  });

  async function open(first: ExportInfo = info()): Promise<ComponentFixture<AccountExport>> {
    const fixture = TestBed.createComponent(AccountExport);
    fixture.detectChanges();
    http.expectOne(URL).flush(first);
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  const query = <T extends Element>(fixture: ComponentFixture<AccountExport>, selector: string) =>
    (fixture.nativeElement as HTMLElement).querySelector<T>(selector);

  /** Run the oldest captured poll and answer its request. */
  function poll(fixture: ComponentFixture<AccountExport>, answer: ExportInfo): void {
    const next = polls.shift();
    expect(next).toBeDefined();
    next!.handler();
    http.expectOne(URL).flush(answer);
    fixture.detectChanges();
  }

  it('explains what the archive contains and what it never contains', async () => {
    const fixture = await open();
    expect(query(fixture, '#export-help')?.textContent).toContain('deleted after 1 day');
    expect(query(fixture, '#export-help')?.textContent).toContain('never included');
    const included = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('#export-included li'),
    ).map((item) => item.textContent?.trim());
    expect(included).toEqual([
      'Profile, preferences and sign-in methods',
      'Workouts, exercise progress and Hevy routines',
    ]);
    expect(query(fixture, '#export-excluded')?.textContent).toContain('OAuth and refresh tokens');
    expect(query<HTMLButtonElement>(fixture, '#export-request')!.disabled).toBe(false);
    expect(query(fixture, '#export-quota')?.textContent).toContain('5 of 5 exports left');
    expect(polls).toHaveLength(0); // nothing is open: no polling
  });

  it('requests an export, follows it while it is built and offers the download', async () => {
    const fixture = await open();

    query<HTMLButtonElement>(fixture, '#export-request')!.click();
    const post = http.expectOne(URL);
    expect(post.request.method).toBe('POST');
    expect(post.request.body).toEqual({});
    post.flush(record(), { status: 202, statusText: 'Accepted' });
    http.expectOne(URL).flush(info([record()], { remaining_today: 4 }));
    fixture.detectChanges();

    // Queued: described, announced, and no second request can be started.
    expect(query(fixture, '#export-latest')?.getAttribute('data-state')).toBe('queued');
    expect(query(fixture, '#export-latest')?.textContent).toContain('Queued');
    expect(query(fixture, '.export-progress')?.getAttribute('aria-label')).toBe('Export progress');
    expect(query(fixture, '#export-status')?.textContent).toContain('queued');
    const button = query<HTMLButtonElement>(fixture, '#export-request')!;
    expect(button.disabled).toBe(true);
    expect(button.textContent).toContain('Export in progress');
    expect(query(fixture, '#export-download')).toBeNull();
    expect(polls.map((item) => item.ms)).toEqual([3000]);

    // Running.
    poll(fixture, info([record({ state: 'running', started_at: '2026-11-12T09:00:05+00:00' })]));
    expect(query(fixture, '#export-latest')?.textContent).toContain('Preparing your export');
    expect(query(fixture, '#export-status')?.textContent).toContain('Preparing');
    expect(polls.map((item) => item.ms)).toEqual([3000]);

    // Ready: a real link to the download route, with size, expiry and checksum.
    poll(fixture, info([ready()], { remaining_today: 4 }));
    const link = query<HTMLAnchorElement>(fixture, '#export-download')!;
    expect(link.getAttribute('href')).toBe(`${URL}/${'a'.repeat(32)}/download`);
    expect(link.hasAttribute('download')).toBe(true);
    expect(link.textContent).toContain('Download ZIP (1.5 MB)');
    const latest = query(fixture, '#export-latest')!.textContent ?? '';
    expect(latest).toContain('Available until');
    expect(latest).toContain('321 records');
    expect(latest).toContain('f'.repeat(64));
    expect(query(fixture, '#export-status')?.textContent).toContain('ready to download');
    expect(query<HTMLButtonElement>(fixture, '#export-request')!.disabled).toBe(false);
    expect(query(fixture, '#export-quota')?.textContent).toContain('4 of 5 exports left');
    expect(polls.map((item) => item.ms)).toEqual([60000]); // watches for expiry only
  });

  it('withdraws the download once the server says the export has expired', async () => {
    const fixture = await open(info([ready()]));
    expect(query(fixture, '#export-download')).not.toBeNull();

    poll(
      fixture,
      info([
        record({
          state: 'expired',
          size_bytes: 1_572_864,
          sha256: 'f'.repeat(64),
          expires_at: '2026-11-13T09:00:09+00:00',
        }),
      ]),
    );

    expect(query(fixture, '#export-download')).toBeNull();
    expect(query(fixture, '#export-latest')?.textContent).toContain('has expired');
    expect(query(fixture, '#export-status')?.textContent).toContain('expired');
    expect(query<HTMLButtonElement>(fixture, '#export-request')!.disabled).toBe(false);
    expect(polls).toHaveLength(0);
  });

  it.each([
    ['too_large', 'larger than this server allows'],
    ['not_eligible', 'suspended or being erased'],
    ['timed_out', 'took too long'],
    ['filesystem_error', 'could not be built'],
    [null, 'could not be built'],
  ])('reports a failed export (%s) without offering a download', async (code, expected) => {
    const fixture = await open(info([record({ state: 'failed', error_code: code })]));
    expect(query(fixture, '#export-failure')?.textContent).toContain(expected);
    expect(query(fixture, '#export-download')).toBeNull();
    expect(query<HTMLButtonElement>(fixture, '#export-request')!.disabled).toBe(false);
    expect(polls).toHaveLength(0);
  });

  it('offers nothing to an account that is not eligible', async () => {
    const fixture = await open(info([], { eligible: false, reason: 'not_eligible' }));
    expect(query(fixture, '#export-request')).toBeNull();
    expect(query(fixture, '#export-ineligible')?.textContent).toContain(
      'signed in with your own account',
    );
  });

  it('disables the request once the daily allowance is used', async () => {
    const fixture = await open(info([ready()], { remaining_today: 0 }));
    expect(query<HTMLButtonElement>(fixture, '#export-request')!.disabled).toBe(true);
    expect(query(fixture, '#export-quota')?.textContent).toContain('0 of 5 exports left');
    fixture.componentInstance.request(); // even a programmatic call is refused
    http.expectNone(URL);
  });

  it('ignores a second click while the request is in flight', async () => {
    const fixture = await open();
    fixture.componentInstance.request();
    fixture.componentInstance.request();
    const posts = http.match((req) => req.method === 'POST');
    expect(posts).toHaveLength(1);
    posts[0].flush(record(), { status: 202, statusText: 'Accepted' });
    http.expectOne(URL).flush(info([record()]));
  });

  it.each([
    [429, 'rate_limited', 'daily export limit'],
    [429, 'Rate limit exceeded', 'Too many requests'],
    [403, 'not_eligible', 'signed in with your own account'],
    [423, 'Account deletion is in progress.', 'being erased right now'],
    [500, 'boom', 'could not be completed'],
  ])('explains a %i response (%s) as an alert and frees the button', async (status, detail, text) => {
    const fixture = await open();
    fixture.componentInstance.request();
    http.expectOne(URL).flush({ detail }, { status, statusText: 'Error' });
    fixture.detectChanges();

    expect(query(fixture, '#export-error')?.getAttribute('role')).toBe('alert');
    expect(query(fixture, '#export-error')?.textContent).toContain(text);
    expect(fixture.componentInstance.busy()).toBe(false);
    expect(query(fixture, '#export-error')?.textContent).not.toContain(detail);
  });

  it('keeps showing what it knows when a refresh fails', async () => {
    const fixture = await open(info([record()]));
    fixture.componentInstance.load();
    http.expectOne(URL).flush({ detail: 'boom' }, { status: 500, statusText: 'Error' });
    fixture.detectChanges();
    expect(query(fixture, '#export-latest')).not.toBeNull();
    expect(query(fixture, '#export-error')).not.toBeNull();
  });

  it('stops polling when the panel is destroyed', async () => {
    const clear = vi.spyOn(globalThis, 'clearTimeout');
    const fixture = await open(info([record()]));
    expect(polls).toHaveLength(1);
    fixture.destroy();
    expect(clear).toHaveBeenCalledWith(1);
  });

  it('is operable and labelled for assistive technology', async () => {
    const fixture = await open(info([ready()]));
    expect(query(fixture, '#export-panel')?.getAttribute('aria-labelledby')).toBe('export-heading');
    expect(query(fixture, '#export-heading')?.tagName).toBe('H3');
    expect(query(fixture, '#export-status')?.getAttribute('role')).toBe('status');
    const describedBy = query(fixture, '#export-request')!.getAttribute('aria-describedby')!;
    for (const id of describedBy.split(' ')) expect(query(fixture, `#${id}`)).not.toBeNull();
    expect(query(fixture, '.export-contents summary')?.textContent).toContain('What the archive');
    expect(query<HTMLAnchorElement>(fixture, '#export-download')!.textContent?.trim()).toMatch(
      /^Download ZIP/,
    );
  });

  it.each([
    [0, '0 B'],
    [512, '512 B'],
    [1536, '1.5 KB'],
    [5 * 1024 * 1024, '5.0 MB'],
    [200 * 1024 * 1024, '200 MB'],
    [3 * 1024 ** 3, '3.0 GB'],
  ])('formats %i bytes as %s', async (bytes, expected) => {
    const fixture = await open();
    expect(fixture.componentInstance.readableSize(bytes)).toBe(expected);
  });
});
