import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Stats } from './stats';

describe('Stats loading errors', () => {
  function setup() {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    return {
      http: TestBed.inject(HttpTestingController),
      stats: TestBed.runInInjectionContext(() => new Stats()),
    };
  }

  it('tells the user when to retry when a quota refuses the report', () => {
    const { http, stats } = setup();
    const detail = 'Reports are limited to 30 per minute. Try again in 20 seconds.';
    stats.load();
    http
      .expectOne('/api/stats')
      .flush({ detail, error: 'quota_exceeded' }, { status: 429, statusText: 'Too Many' });
    expect(stats.error()).toBe(detail);
    expect(stats.loading()).toBe(false);
  });

  it('keeps the generic message for other failures and clears it on success', () => {
    const { http, stats } = setup();
    stats.load();
    http.expectOne('/api/stats').flush({ detail: 'boom' }, { status: 500, statusText: 'Error' });
    expect(stats.error()).toBe('Failed to load stats data');

    stats.load();
    http.expectOne('/api/stats').flush({ prs: [] });
    expect(stats.error()).toBeNull();
    expect(stats.data()).toEqual({ prs: [] });
  });
});
