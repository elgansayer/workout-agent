import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Dashboard } from './dashboard';

describe('Dashboard refresh', () => {
  it('reloads the next workout when returning to an already-open app', () => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    const http = TestBed.inject(HttpTestingController);
    const dashboard = TestBed.runInInjectionContext(() => new Dashboard());

    dashboard.ngOnInit();
    http.expectOne('/api/dashboard').flush({ focus: 'Upper A' });
    expect(dashboard.data().focus).toBe('Upper A');

    dashboard.onWindowFocus();
    http.expectOne('/api/dashboard').flush({ focus: 'Upper B' });
    expect(dashboard.data().focus).toBe('Upper B');
    expect(dashboard.loading()).toBe(false);
    http.verify();
  });
});
