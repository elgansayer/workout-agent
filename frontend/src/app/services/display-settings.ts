import { DOCUMENT } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { Injectable, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';

export type UnitSystem = 'metric' | 'imperial';
export type MotionPreference = 'system' | 'reduce';
export type ContrastPreference = 'system' | 'more';
export type TextSize = 'default' | 'large' | 'x-large';

/** The signed-in user's regional and accessibility preferences. */
export interface DisplaySettings {
  timezone: string;
  locale: string;
  unit_system: UnitSystem;
  motion: MotionPreference;
  contrast: ContrastPreference;
  text_size: TextSize;
}

export interface OptionChoice {
  value: string;
  label: string;
}

/** Everything the server accepts, so the UI never hard-codes the lists. */
export interface DisplayOptions {
  defaults: DisplaySettings;
  timezones: string[];
  locales: OptionChoice[];
  unit_systems: OptionChoice[];
  motion: OptionChoice[];
  contrast: OptionChoice[];
  text_size: OptionChoice[];
}

export interface DisplaySettingsSaved {
  status: string;
  settings: DisplaySettings;
  changed: string[];
  regrouped_days: number;
}

export type DateStyle = 'shortDate' | 'medium' | 'shortTime';

/**
 * Explicit defaults, identical to the backend's. They apply until the server's
 * values arrive and whenever the user is signed out or offline.
 */
export const DEFAULT_DISPLAY_SETTINGS: DisplaySettings = {
  timezone: 'UTC',
  locale: 'en-GB',
  unit_system: 'metric',
  motion: 'system',
  contrast: 'system',
  text_size: 'default',
};

const FALLBACK_LOCALE = DEFAULT_DISPLAY_SETTINGS.locale;
const FALLBACK_TIMEZONE = DEFAULT_DISPLAY_SETTINGS.timezone;
const CALENDAR_DAY = /^(\d{4})-(\d{2})-(\d{2})$/;

const DATE_STYLES: Record<DateStyle, Intl.DateTimeFormatOptions> = {
  shortDate: { dateStyle: 'short' },
  medium: { dateStyle: 'medium', timeStyle: 'short' },
  shortTime: { timeStyle: 'short' },
};

@Injectable({ providedIn: 'root' })
export class DisplaySettingsService {
  private readonly http = inject(HttpClient);
  private readonly document = inject(DOCUMENT);
  private readonly dateFormats = new Map<string, Intl.DateTimeFormat>();
  private readonly numberFormats = new Map<string, Intl.NumberFormat>();
  private readonly dayParts = new Map<string, Intl.DateTimeFormat>();

  readonly settings = signal<DisplaySettings>(DEFAULT_DISPLAY_SETTINGS);
  readonly options = signal<DisplayOptions | null>(null);

  /** Fetch the user's settings; signed-out or offline keeps the explicit defaults. */
  async load(): Promise<void> {
    try {
      const response = await firstValueFrom(
        this.http.get<{ settings: DisplaySettings; options: DisplayOptions }>(
          '/api/settings/display',
        ),
      );
      this.options.set(response.options);
      this.apply(response.settings);
    } catch {
      this.apply(this.settings());
    }
  }

  /** Persist a partial update. Rejects with the server's HttpErrorResponse on invalid input. */
  async save(changes: Partial<DisplaySettings>): Promise<DisplaySettingsSaved> {
    const saved = await firstValueFrom(
      this.http.post<DisplaySettingsSaved>('/api/settings/display', changes),
    );
    this.apply(saved.settings);
    return saved;
  }

  /** Make the settings current for formatting and expose accessibility choices to CSS. */
  apply(settings: DisplaySettings): void {
    this.settings.set(settings);
    const root = this.document.documentElement;
    root.lang = settings.locale;
    root.dataset['motion'] = settings.motion;
    root.dataset['contrast'] = settings.contrast;
    root.dataset['textSize'] = settings.text_size;
  }

  /**
   * Format an instant in the user's zone or a calendar day as-is.
   *
   * A bare `YYYY-MM-DD` is a calendar day, not a moment, so it is never shifted
   * between zones. Anything with a time is converted to the user's time zone.
   */
  formatDate(value: unknown, style: DateStyle = 'shortDate'): string {
    if (value === null || value === undefined || value === '') return '';
    const { locale, timezone } = this.settings();
    if (typeof value === 'string') {
      const day = CALENDAR_DAY.exec(value);
      if (day) {
        const moment = new Date(Date.UTC(+day[1], +day[2] - 1, +day[3]));
        const dayStyle: DateStyle = style === 'shortTime' ? 'shortDate' : style;
        return this.dateFormat(locale, 'UTC', dayStyle, true).format(moment);
      }
    }
    const moment = value instanceof Date ? value : new Date(value as string | number);
    if (Number.isNaN(moment.getTime())) return String(value);
    return this.dateFormat(locale, timezone, style, false).format(moment);
  }

  /** The current calendar day (`YYYY-MM-DD`) in the user's own time zone, not the browser's or UTC. */
  localToday(now: Date = new Date()): string {
    const { timezone } = this.settings();
    let format = this.dayParts.get(timezone);
    if (!format) {
      const options: Intl.DateTimeFormatOptions = {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
      };
      try {
        format = new Intl.DateTimeFormat('en-US', { ...options, timeZone: timezone });
      } catch {
        format = new Intl.DateTimeFormat('en-US', { ...options, timeZone: FALLBACK_TIMEZONE });
      }
      this.dayParts.set(timezone, format);
    }
    const part = (type: string) => format!.formatToParts(now).find((p) => p.type === type)!.value;
    return `${part('year')}-${part('month')}-${part('day')}`;
  }

  formatNumber(value: number | null | undefined, maximumFractionDigits = 0): string {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '';
    return this.numberFormat(this.settings().locale, maximumFractionDigits).format(Number(value));
  }

  private dateFormat(
    locale: string,
    timeZone: string,
    style: DateStyle,
    calendarDay: boolean,
  ): Intl.DateTimeFormat {
    const key = `${locale}|${timeZone}|${style}|${calendarDay}`;
    let format = this.dateFormats.get(key);
    if (!format) {
      const options: Intl.DateTimeFormatOptions = calendarDay
        ? { dateStyle: DATE_STYLES[style].dateStyle }
        : DATE_STYLES[style];
      try {
        format = new Intl.DateTimeFormat(locale, { ...options, timeZone });
      } catch {
        // A locale or zone this browser does not know must not break a page.
        format = new Intl.DateTimeFormat(FALLBACK_LOCALE, {
          ...options,
          timeZone: FALLBACK_TIMEZONE,
        });
      }
      this.dateFormats.set(key, format);
    }
    return format;
  }

  private numberFormat(locale: string, maximumFractionDigits: number): Intl.NumberFormat {
    const key = `${locale}|${maximumFractionDigits}`;
    let format = this.numberFormats.get(key);
    if (!format) {
      try {
        format = new Intl.NumberFormat(locale, { maximumFractionDigits });
      } catch {
        format = new Intl.NumberFormat(FALLBACK_LOCALE, { maximumFractionDigits });
      }
      this.numberFormats.set(key, format);
    }
    return format;
  }
}
