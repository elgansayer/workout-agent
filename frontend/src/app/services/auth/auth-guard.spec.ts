import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import {
  ActivatedRouteSnapshot,
  Router,
  RouterStateSnapshot,
  UrlTree,
  provideRouter,
} from '@angular/router';

import { Auth } from '../auth';
import { authGuard } from './auth-guard';

describe('authGuard', () => {
  let auth: Auth;
  let http: HttpTestingController;
  let router: Router;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    });
    auth = TestBed.inject(Auth);
    http = TestBed.inject(HttpTestingController);
    router = TestBed.inject(Router);
  });

  afterEach(() => http.verify());

  function run(url: string): Promise<boolean | UrlTree> {
    const route = {} as ActivatedRouteSnapshot;
    const state = { url } as RouterStateSnapshot;
    return TestBed.runInInjectionContext(() => authGuard(route, state)) as Promise<
      boolean | UrlTree
    >;
  }

  it('lets a signed-in visitor through without asking again', async () => {
    auth.currentUser.set({ id: 'user-1', email: 'athlete@example.test' });

    expect(await run('/plan')).toBe(true);
  });

  it('sends a signed-out visitor to sign in and remembers where they were headed', async () => {
    auth.currentUser.set(null);

    const outcome = await run('/plan?week=2');

    expect(outcome).toBeInstanceOf(UrlTree);
    expect(router.serializeUrl(outcome as UrlTree)).toBe('/login?next=%2Fplan%3Fweek%3D2');
  });

  it('checks the session first when it is not known yet', async () => {
    const pending = run('/settings#hevy');

    http
      .expectOne('/api/me')
      .flush({ error: 'Unauthorized' }, { status: 401, statusText: 'Unauthorized' });
    const outcome = await pending;

    expect(router.serializeUrl(outcome as UrlTree)).toBe('/login?next=%2Fsettings%23hevy');
  });

  it('admits a visitor whose session turns out to be valid', async () => {
    const pending = run('/plan');

    http.expectOne('/api/me').flush({ user: { id: 'user-1', email: 'athlete@example.test' } });

    expect(await pending).toBe(true);
  });

  it('adds nothing for the default page or an unsafe target', async () => {
    auth.currentUser.set(null);

    for (const url of ['/', '//evil.example', '/login', '/api/me']) {
      const outcome = await run(url);
      expect(router.serializeUrl(outcome as UrlTree)).toBe('/login');
    }
  });
});
