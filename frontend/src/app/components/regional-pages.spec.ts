import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { Type } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { DEFAULT_DISPLAY_SETTINGS, DisplaySettingsService } from '../services/display-settings';
import { Checkins } from './checkins/checkins';
import { History } from './history/history';
import { Progress } from './progress/progress';
import { Stats } from './stats/stats';

/**
 * Pages must show the owner's units (supplied by the API, which converts the
 * stored kilograms) and render dates in the owner's locale and time zone.
 */
describe('regional page rendering', () => {
  let http: HttpTestingController;
  let settings: DisplaySettingsService;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
    settings = TestBed.inject(DisplaySettingsService);
  });

  afterEach(() => {
    http.verify();
    settings.apply(DEFAULT_DISPLAY_SETTINGS);
  });

  async function render<T>(
    component: Type<T>,
    url: string,
    body: object,
  ): Promise<{ fixture: ComponentFixture<T>; text: string }> {
    const fixture = TestBed.createComponent(component);
    fixture.detectChanges();
    http.expectOne(url).flush(body);
    await fixture.whenStable();
    fixture.detectChanges();
    return {
      fixture,
      text: (fixture.nativeElement as HTMLElement).textContent!.replace(/\s+/g, ' '),
    };
  }

  const stats = (unit: string, volume: number, e1rm: string) => ({
    total_sessions: 2,
    total_volume: volume,
    weight_unit: unit,
    days_on_programme: 10,
    exercises_tracked: 1,
    projections: [],
    prs: [{ exercise: 'Deadlift', e1rm, detail: `150 ${unit} x 5`, date: '2026-03-09' }],
  });

  it('labels stats in kilograms by default', async () => {
    const { text } = await render(Stats, '/api/stats', stats('kg', 12345, '175'));
    expect(text).toContain('12,345');
    expect(text).toContain('kg moved');
    expect(text).toContain('175 kg');
  });

  it('labels stats in pounds and uses the owner locale for numbers and dates', async () => {
    settings.apply({ ...DEFAULT_DISPLAY_SETTINGS, unit_system: 'imperial', locale: 'en-US' });
    const { text } = await render(Stats, '/api/stats', stats('lb', 27216.1, '385.8'));

    expect(text).toContain('27,216');
    expect(text).toContain('lb moved');
    expect(text).toContain('385.8 lb');
    expect(text).not.toContain('kg');
    // The record date is a calendar day: shown as stored, in the US order.
    expect(text).toContain('3/9/26');
  });

  it('labels progress charts with the unit the API converted to', async () => {
    const body = {
      charts: [{ name: 'Deadlift', svg: '<svg></svg>', best_e1rm: 385.8, sessions: 2 }],
      body: { weight: '<svg></svg>', body_fat: null, muscle: null, resting_hr: null },
      weight_unit: 'lb',
    };
    const { text } = await render(Progress, '/api/progress', body);

    expect(text).toContain('Weight (lb)');
    expect(text).toContain('e1RM 385.8 lb');
    expect(text).not.toContain('kg');
  });

  it('shows history dates as stored calendar days in the owner locale', async () => {
    settings.apply({
      ...DEFAULT_DISPLAY_SETTINGS,
      locale: 'en-US',
      timezone: 'Pacific/Auckland',
    });
    const { text } = await render(History, '/api/history', {
      logs: [{ date: '2026-03-02', day: 1, focus: 'Back', carb_tier: 'high', plan: 'p' }],
      calendar: '<svg></svg>',
    });

    expect(text).toContain('3/2/26');
  });

  it('shows check-in dates in the owner locale', async () => {
    settings.apply({ ...DEFAULT_DISPLAY_SETTINGS, locale: 'en-US' });
    const { text } = await render(Checkins, '/api/checkins', {
      checkins: [
        { number: 1, date: '2026-03-02', workouts_done: 24, weeks: 4, message: 'Solid block.' },
      ],
      loading_svg: null,
    });

    expect(text).toContain('3/2/26');
  });
});
