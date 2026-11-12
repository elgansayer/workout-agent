import {
  HttpClient,
  HttpErrorResponse,
  provideHttpClient,
  withInterceptors,
} from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import type { MockInstance } from 'vitest';

import { Auth } from '../services/auth';
import { apiInterceptor } from './api.interceptor';

describe('apiInterceptor session handling', () => {
  const user = { id: 'user-1', email: 'athlete@example.test' };
  let http: HttpClient;
  let controller: HttpTestingController;
  let auth: Auth;
  let router: Router;
  let navigate: MockInstance<Router['navigate']>;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        provideHttpClient(withInterceptors([apiInterceptor])),
        provideHttpClientTesting(),
      ],
    });
    http = TestBed.inject(HttpClient);
    controller = TestBed.inject(HttpTestingController);
    auth = TestBed.inject(Auth);
    router = TestBed.inject(Router);
    navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);
    auth.currentUser.set(user);
  });

  afterEach(() => controller.verify());

  async function failure(call: Promise<unknown>): Promise<HttpErrorResponse> {
    try {
      await call;
    } catch (error) {
      return error as HttpErrorResponse;
    }
    throw new Error('the request was expected to fail');
  }

  const unauthorised = { status: 401, statusText: 'Unauthorized' };

  it('returns to sign-in when the API says the session ended', async () => {
    const outcome = failure(
      new Promise((resolve, reject) =>
        http.get('/api/settings').subscribe({ next: resolve, error: reject }),
      ),
    );

    controller.expectOne('/api/settings').flush({ detail: 'Not authenticated' }, unauthorised);

    expect((await outcome).status).toBe(401); // callers still see the error
    expect(auth.currentUser()).toBeNull();
    expect(navigate).toHaveBeenCalledWith(['/login']);
  });

  it('returns to the page whose request found the session ended', async () => {
    vi.spyOn(router, 'url', 'get').mockReturnValue('/plan?week=2');
    const outcome = failure(
      new Promise((resolve, reject) =>
        http.get('/api/settings').subscribe({ next: resolve, error: reject }),
      ),
    );

    controller.expectOne('/api/settings').flush({ detail: 'Not authenticated' }, unauthorised);

    expect((await outcome).status).toBe(401);
    expect(navigate).toHaveBeenCalledWith(['/login'], {
      queryParams: { next: '/plan?week=2' },
    });
  });

  it('sends credentials with every backend request', () => {
    http.get('/api/settings').subscribe();

    const request = controller.expectOne('/api/settings');
    expect(request.request.withCredentials).toBe(true);
    request.flush({});
    expect(navigate).not.toHaveBeenCalled();
  });

  it('handles an ended session discovered while sending a protected mutation', async () => {
    const outcome = failure(
      new Promise((resolve, reject) =>
        http.post('/api/settings/key', {}).subscribe({ next: resolve, error: reject }),
      ),
    );

    controller.expectOne('/api/csrf-token').flush({ token: 'one-time', expires_in: 600 });
    const mutation = controller.expectOne('/api/settings/key');
    expect(mutation.request.headers.get('X-CSRF-Token')).toBe('one-time');
    mutation.flush({ detail: 'Not authenticated' }, unauthorised);

    expect((await outcome).status).toBe(401);
    expect(auth.currentUser()).toBeNull();
    expect(navigate).toHaveBeenCalledWith(['/login']);
  });

  it('handles an ended session discovered while fetching the CSRF token', async () => {
    const outcome = failure(
      new Promise((resolve, reject) =>
        http.post('/api/auth/logout-all', {}).subscribe({ next: resolve, error: reject }),
      ),
    );

    controller.expectOne('/api/csrf-token').flush({ detail: 'Not authenticated' }, unauthorised);

    expect((await outcome).status).toBe(401);
    expect(auth.currentUser()).toBeNull();
    expect(navigate).toHaveBeenCalledWith(['/login']);
  });

  it('leaves the auth probe alone: a signed-out visitor is expected to get 401', async () => {
    const outcome = failure(
      new Promise((resolve, reject) =>
        http.get('/api/me?probe=1').subscribe({ next: resolve, error: reject }),
      ),
    );

    controller.expectOne('/api/me?probe=1').flush({ error: 'Unauthorized' }, unauthorised);

    await outcome;
    expect(auth.currentUser()).toEqual(user);
    expect(navigate).not.toHaveBeenCalled();
  });

  it('does not navigate when the visitor is already on the sign-in page', async () => {
    vi.spyOn(router, 'url', 'get').mockReturnValue('/login');
    const outcome = failure(
      new Promise((resolve, reject) =>
        http.get('/api/settings').subscribe({ next: resolve, error: reject }),
      ),
    );

    controller.expectOne('/api/settings').flush({}, unauthorised);

    await outcome;
    expect(auth.currentUser()).toBeNull();
    expect(navigate).not.toHaveBeenCalled();
  });

  it('ignores other failures', async () => {
    const outcome = failure(
      new Promise((resolve, reject) =>
        http.get('/api/settings').subscribe({ next: resolve, error: reject }),
      ),
    );

    controller.expectOne('/api/settings').flush({}, { status: 500, statusText: 'Server Error' });

    expect((await outcome).status).toBe(500);
    expect(auth.currentUser()).toEqual(user);
    expect(navigate).not.toHaveBeenCalled();
  });

  it('never treats a third-party 401 as the end of our own session', async () => {
    const outcome = failure(
      new Promise((resolve, reject) =>
        http.get('https://third-party.example/data').subscribe({ next: resolve, error: reject }),
      ),
    );

    controller.expectOne('https://third-party.example/data').flush({}, unauthorised);

    await outcome;
    expect(auth.currentUser()).toEqual(user);
    expect(navigate).not.toHaveBeenCalled();
  });
});
