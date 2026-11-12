import { Pipe, PipeTransform, inject } from '@angular/core';

import { DateStyle, DisplaySettingsService } from '../services/display-settings';

/**
 * Formats a date in the signed-in user's locale and time zone. Impure on
 * purpose: changing the preference must repaint without a page reload.
 */
@Pipe({ name: 'localDate', pure: false })
export class LocalDatePipe implements PipeTransform {
  private readonly settings = inject(DisplaySettingsService);

  transform(value: unknown, style: DateStyle = 'shortDate'): string {
    return this.settings.formatDate(value, style);
  }
}

/** Formats a number using the signed-in user's locale conventions. */
@Pipe({ name: 'localNumber', pure: false })
export class LocalNumberPipe implements PipeTransform {
  private readonly settings = inject(DisplaySettingsService);

  transform(value: number | null | undefined, maximumFractionDigits = 0): string {
    return this.settings.formatNumber(value, maximumFractionDigits);
  }
}
