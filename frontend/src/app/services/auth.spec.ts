import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import type { Mock } from 'vitest';

import { Auth } from './auth';

describe('Auth', () => {
  let service: Auth;

  beforeEach(() => {
    TestBed.configureTestingModule({});
    service = TestBed.inject(Auth);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });
});

describe('Auth session lifecycle', () => {
  const user = { id: 'user-1', email: 'athlete@example.test' };
  let service: Auth;
  let http: HttpTestingController;
  let navigate: Mock<(url: string) => void>;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(Auth);
    http = TestBed.inject(HttpTestingController);
    navigate = vi.fn<(url: string) => void>();
    // A real navigation would reload the test page; observe it instead.
    (service as unknown as { navigate: (url: string) => void }).navigate = navigate;
    service.currentUser.set(user);
  });

  afterEach(() => http.verify());

  it('signs out through the API and only then returns to sign-in', async () => {
    const done = service.logout();

    const request = http.expectOne('/api/auth/logout');
    expect(request.request.method).toBe('POST');
    expect(navigate).not.toHaveBeenCalled();
    request.flush({ status: 'ok' });
    await done;

    expect(service.currentUser()).toBeNull();
    expect(navigate).toHaveBeenCalledTimes(1);
    expect(navigate).toHaveBeenCalledWith('/login');
  });

  it('still returns to sign-in when the session had already ended (401)', async () => {
    const done = service.logout();

    http.expectOne('/api/auth/logout').flush(null, { status: 401, statusText: 'Unauthorized' });
    await done;

    expect(service.currentUser()).toBeNull();
    expect(navigate).toHaveBeenCalledWith('/login');
  });

  it('does not claim to be signed out when the server could not be reached', async () => {
    const done = service.logout();

    http.expectOne('/api/auth/logout').error(new ProgressEvent('error'));
    await done;

    // The session may still be alive server-side, so keep the user's state and
    // fall back to the full-page sign-out, which visibly fails if still offline.
    expect(service.currentUser()).toEqual(user);
    expect(navigate).toHaveBeenCalledTimes(1);
    expect(navigate).toHaveBeenCalledWith('/logout');
  });

  it('signs out of every device through the API and then returns to sign-in', async () => {
    const done = service.logoutAllDevices();

    const request = http.expectOne('/api/auth/logout-all');
    expect(request.request.method).toBe('POST');
    request.flush({ status: 'ok', revoked: 2 });
    await done;

    expect(service.currentUser()).toBeNull();
    expect(navigate).toHaveBeenCalledWith('/login');
  });

  it('reports a failed sign-out of all devices instead of pretending it worked', async () => {
    const done = service.logoutAllDevices();
    const outcome = expect(done).rejects.toBeTruthy();

    http.expectOne('/api/auth/logout-all').flush(null, { status: 500, statusText: 'Server Error' });
    await outcome;

    expect(service.currentUser()).toEqual(user);
    expect(navigate).not.toHaveBeenCalled();
  });

  it('forgets the user when the API reports the session ended', () => {
    service.markSessionEnded();

    expect(service.currentUser()).toBeNull();
  });
});
