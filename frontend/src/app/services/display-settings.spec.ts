import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import {
  DEFAULT_DISPLAY_SETTINGS,
  DisplayOptions,
  DisplaySettings,
  DisplaySettingsService,
} from './display-settings';

const options: DisplayOptions = {
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
  motion: [{ value: 'system', label: 'Follow my device setting' }],
  contrast: [{ value: 'system', label: 'Follow my device setting' }],
  text_size: [{ value: 'default', label: 'Default' }],
};

const settings = (changes: Partial<DisplaySettings> = {}): DisplaySettings => ({
  ...DEFAULT_DISPLAY_SETTINGS,
  ...changes,
});

describe('DisplaySettingsService', () => {
  let service: DisplaySettingsService;
  let http: HttpTestingController;
  const root = document.documentElement;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(DisplaySettingsService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
    root.removeAttribute('lang');
    delete root.dataset['motion'];
    delete root.dataset['contrast'];
    delete root.dataset['textSize'];
  });

  describe('defaults and loading', () => {
    it('starts on the explicit defaults', () => {
      expect(service.settings()).toEqual({
        timezone: 'UTC',
        locale: 'en-GB',
        unit_system: 'metric',
        motion: 'system',
        contrast: 'system',
        text_size: 'default',
      });
      expect(service.options()).toBeNull();
    });

    it("loads the signed-in user's settings and the accepted options", async () => {
      const loading = service.load();
      const request = http.expectOne('/api/settings/display');
      expect(request.request.method).toBe('GET');
      request.flush({
        settings: settings({ timezone: 'Pacific/Auckland', unit_system: 'imperial' }),
        options,
      });
      await loading;

      expect(service.settings().timezone).toBe('Pacific/Auckland');
      expect(service.options()).toEqual(options);
    });

    it('keeps the explicit defaults when signed out or offline', async () => {
      const loading = service.load();
      http.expectOne('/api/settings/display').flush(
        { detail: 'Unauthorized' },
        {
          status: 401,
          statusText: 'Unauthorized',
        },
      );
      await loading;

      expect(service.settings()).toEqual(DEFAULT_DISPLAY_SETTINGS);
      expect(root.lang).toBe('en-GB');
    });
  });

  describe('applying preferences to the page', () => {
    it('mirrors language and accessibility choices onto <html> for CSS and assistive tech', () => {
      service.apply(
        settings({ locale: 'en-US', motion: 'reduce', contrast: 'more', text_size: 'x-large' }),
      );

      expect(root.lang).toBe('en-US');
      expect(root.dataset['motion']).toBe('reduce');
      expect(root.dataset['contrast']).toBe('more');
      expect(root.dataset['textSize']).toBe('x-large');
      expect(root.getAttribute('data-text-size')).toBe('x-large');
    });

    it('can be switched back to following the device', () => {
      service.apply(settings({ motion: 'reduce' }));
      service.apply(settings());
      expect(root.dataset['motion']).toBe('system');
    });
  });

  describe('saving', () => {
    it('posts only the supplied fields and applies the saved result', async () => {
      const saving = service.save({ unit_system: 'imperial', motion: 'reduce' });
      const request = http.expectOne('/api/settings/display');
      expect(request.request.method).toBe('POST');
      expect(request.request.body).toEqual({ unit_system: 'imperial', motion: 'reduce' });
      request.flush({
        status: 'ok',
        settings: settings({ unit_system: 'imperial', motion: 'reduce' }),
        changed: ['unit_system', 'motion'],
        regrouped_days: 0,
      });

      const saved = await saving;
      expect(saved.changed).toEqual(['unit_system', 'motion']);
      expect(root.dataset['motion']).toBe('reduce');
    });

    it('rejects with the server error and leaves the current settings alone', async () => {
      const saving = service.save({ locale: 'fr-FR' });
      http
        .expectOne('/api/settings/display')
        .flush(
          { detail: 'Unsupported locale. Supported locales: en-GB, en-US.' },
          { status: 400, statusText: 'Bad Request' },
        );

      await expect(saving).rejects.toMatchObject({ status: 400 });
      expect(service.settings().locale).toBe('en-GB');
    });
  });

  describe('dates', () => {
    it('renders the same instant on different days in different time zones', () => {
      const instant = '2026-03-02T20:30:00+00:00';

      service.apply(settings({ timezone: 'UTC' }));
      expect(service.formatDate(instant, 'shortDate')).toBe('02/03/2026');
      service.apply(settings({ timezone: 'Pacific/Auckland' }));
      expect(service.formatDate(instant, 'shortDate')).toBe('03/03/2026');
      service.apply(settings({ timezone: 'America/Los_Angeles' }));
      expect(service.formatDate(instant, 'shortDate')).toBe('02/03/2026');
    });

    it('applies the daylight-saving offset in force at each instant', () => {
      service.apply(settings({ timezone: 'America/New_York' }));

      // 00:30 EDT on 9 March (a fixed EST offset would still say the 8th).
      expect(service.formatDate('2026-03-09T04:30:00Z', 'shortDate')).toBe('09/03/2026');
      // 23:30 EST on 1 November (a fixed EDT offset would already say the 2nd).
      expect(service.formatDate('2026-11-02T04:30:00Z', 'shortDate')).toBe('01/11/2026');
      // The repeated 01:30 hour of the fall-back night, on both sides of the change.
      expect(service.formatDate('2026-11-01T05:30:00Z', 'shortTime')).toBe('01:30');
      expect(service.formatDate('2026-11-01T06:30:00Z', 'shortTime')).toBe('01:30');
      // Clock times shift by an hour across the spring change: 12:00Z is 07:00 EST, then 08:00 EDT.
      expect(service.formatDate('2026-03-07T12:00:00Z', 'shortTime')).toBe('07:00');
      expect(service.formatDate('2026-03-08T12:00:00Z', 'shortTime')).toBe('08:00');
    });

    it('never shifts a bare calendar day, whatever the time zone', () => {
      for (const timezone of ['Pacific/Auckland', 'America/Los_Angeles', 'UTC']) {
        service.apply(settings({ timezone }));
        expect(service.formatDate('2026-03-02', 'shortDate')).toBe('02/03/2026');
      }
    });

    it('follows the locale', () => {
      service.apply(settings({ locale: 'en-US', timezone: 'UTC' }));
      expect(service.formatDate('2026-03-02', 'shortDate')).toBe('3/2/26');
      service.apply(settings({ locale: 'en-GB', timezone: 'UTC' }));
      expect(service.formatDate('2026-03-02', 'shortDate')).toBe('02/03/2026');
    });

    it('formats dates and times together for the medium style', () => {
      service.apply(settings({ timezone: 'Europe/London' }));
      expect(service.formatDate('2026-06-15T06:00:00Z', 'medium')).toContain('07:00');
      expect(service.formatDate('2026-06-15', 'medium')).not.toContain(':');
    });

    it('renders nothing for empty input and echoes unparseable input', () => {
      expect(service.formatDate(null)).toBe('');
      expect(service.formatDate(undefined)).toBe('');
      expect(service.formatDate('')).toBe('');
      expect(service.formatDate('not a date')).toBe('not a date');
    });

    it('falls back to the defaults for a zone or locale the browser rejects', () => {
      service.apply(settings({ timezone: 'Mars/Base', locale: 'xx-INVALID-TAG-' }));
      expect(service.formatDate('2026-03-02T20:30:00Z', 'shortDate')).toBe('02/03/2026');
      expect(service.formatNumber(1234.5, 1)).toBe('1,234.5');
    });
  });

  describe('today', () => {
    it("is the user's calendar day, not UTC's", () => {
      const instant = new Date('2026-03-02T20:30:00Z');

      service.apply(settings({ timezone: 'UTC' }));
      expect(service.localToday(instant)).toBe('2026-03-02');
      service.apply(settings({ timezone: 'Pacific/Auckland' }));
      expect(service.localToday(instant)).toBe('2026-03-03');
      service.apply(settings({ timezone: 'Pacific/Honolulu' }));
      expect(service.localToday(instant)).toBe('2026-03-02');
    });

    it('respects daylight saving at the boundary', () => {
      service.apply(settings({ timezone: 'America/New_York' }));
      expect(service.localToday(new Date('2026-03-09T04:30:00Z'))).toBe('2026-03-09');
      expect(service.localToday(new Date('2026-03-09T03:30:00Z'))).toBe('2026-03-08');
      expect(service.localToday(new Date('2026-11-02T04:30:00Z'))).toBe('2026-11-01');
      expect(service.localToday(new Date('2026-11-02T05:00:00Z'))).toBe('2026-11-02');
    });

    it('falls back to UTC for a zone the browser rejects', () => {
      service.apply(settings({ timezone: 'Mars/Base' }));
      expect(service.localToday(new Date('2026-03-02T23:30:00Z'))).toBe('2026-03-02');
    });
  });

  describe('numbers', () => {
    it('uses the locale grouping and a bounded number of decimals', () => {
      service.apply(settings({ locale: 'en-GB' }));
      expect(service.formatNumber(12345.678, 0)).toBe('12,346');
      expect(service.formatNumber(12345.678, 1)).toBe('12,345.7');
      expect(service.formatNumber(null)).toBe('');
      expect(service.formatNumber(undefined)).toBe('');
      expect(service.formatNumber(Number.NaN)).toBe('');
    });
  });
});
