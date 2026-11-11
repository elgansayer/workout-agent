import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Progress } from './progress';

const QUOTA_DETAIL = 'Reports are limited to 30 per minute. Try again in 12 seconds.';

describe('Progress explanations', () => {
  it('ignores a late response after closing and reopening an explanation', () => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    const http = TestBed.inject(HttpTestingController);
    const progress = TestBed.runInInjectionContext(() => new Progress());
    progress.toggleXAI('Squat');
    const old = http.expectOne((request) => request.url.includes('/api/xai_reasoning/'));
    progress.toggleXAI('Squat');
    progress.toggleXAI('Squat');
    const fresh = http.expectOne((request) => request.url.includes('/api/xai_reasoning/'));
    fresh.flush({ reasoning: 'Current explanation' });
    old.flush({ reasoning: 'Obsolete explanation' });
    expect(progress.xaiReasoning()['Squat']).toBe('Current explanation');
    progress.toggleXAI('Squat');
    expect(progress.xaiReasoning()['Squat']).toBeUndefined();
    http.verify();
  });
});

describe('Progress quota messages', () => {
  function setup() {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    return {
      http: TestBed.inject(HttpTestingController),
      progress: TestBed.runInInjectionContext(() => new Progress()),
    };
  }

  it('tells the user when to retry when loading is refused by a quota', () => {
    const { http, progress } = setup();
    progress.load();
    http
      .expectOne('/api/progress')
      .flush(
        { detail: QUOTA_DETAIL, error: 'quota_exceeded' },
        { status: 429, statusText: 'Too Many' },
      );
    expect(progress.error()).toBe(QUOTA_DETAIL);
    expect(progress.loading()).toBe(false);
  });

  it('keeps the generic message for failures that are not quota refusals', () => {
    const { http, progress } = setup();
    progress.load();
    http.expectOne('/api/progress').flush({ detail: 'boom' }, { status: 500, statusText: 'Error' });
    expect(progress.error()).toBe('Failed to load progress data');
  });

  it('explains a quota refusal instead of a generic analysis error', () => {
    const { http, progress } = setup();
    progress.toggleXAI('Squat');
    http
      .expectOne((request) => request.url.includes('/api/xai_reasoning/'))
      .flush(
        {
          detail: 'AI requests are limited to 15 per minute. Try again in 9 seconds.',
          error: 'quota_exceeded',
        },
        { status: 429, statusText: 'Too Many' },
      );
    expect(progress.xaiReasoning()['Squat']).toContain('Try again in 9 seconds');
  });
});
