import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Progress } from './progress';

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
