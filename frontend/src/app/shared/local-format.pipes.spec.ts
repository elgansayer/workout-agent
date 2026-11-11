import { Component, signal } from '@angular/core';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { DEFAULT_DISPLAY_SETTINGS, DisplaySettingsService } from '../services/display-settings';
import { LocalDatePipe, LocalNumberPipe } from './local-format.pipes';

@Component({
  imports: [LocalDatePipe, LocalNumberPipe],
  template: `
    <span id="day">{{ day() | localDate: 'shortDate' }}</span>
    <span id="instant">{{ instant() | localDate: 'shortDate' }}</span>
    <span id="clock">{{ instant() | localDate: 'shortTime' }}</span>
    <span id="volume">{{ volume() | localNumber }}</span>
  `,
})
class Host {
  day = signal('2026-03-02');
  instant = signal('2026-03-02T20:30:00+00:00');
  volume = signal(12345.6);
}

describe('regional format pipes', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
  });

  const text = (element: HTMLElement, id: string) =>
    element.querySelector(`#${id}`)?.textContent?.trim();

  it('repaint immediately when the saved preference changes', () => {
    const settings = TestBed.inject(DisplaySettingsService);
    const fixture = TestBed.createComponent(Host);
    fixture.detectChanges();
    const element = fixture.nativeElement as HTMLElement;

    expect(text(element, 'instant')).toBe('02/03/2026');
    expect(text(element, 'clock')).toBe('20:30');
    expect(text(element, 'volume')).toBe('12,346');

    settings.apply({ ...DEFAULT_DISPLAY_SETTINGS, timezone: 'Pacific/Auckland', locale: 'en-US' });
    fixture.detectChanges();

    expect(text(element, 'instant')).toBe('3/3/26');
    expect(text(element, 'clock')).toBe('9:30 AM');
  });

  it('shows a calendar day exactly as stored in every zone', () => {
    const settings = TestBed.inject(DisplaySettingsService);
    const fixture = TestBed.createComponent(Host);
    const element = fixture.nativeElement as HTMLElement;

    for (const timezone of ['Pacific/Kiritimati', 'Pacific/Pago_Pago', 'UTC']) {
      settings.apply({ ...DEFAULT_DISPLAY_SETTINGS, timezone });
      fixture.detectChanges();
      expect(text(element, 'day')).toBe('02/03/2026');
    }
  });
});
