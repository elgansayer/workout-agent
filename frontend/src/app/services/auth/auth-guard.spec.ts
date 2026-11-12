import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import {
  ActivatedRouteSnapshot,
  Router,
  RouterStateSnapshot,
  provideRouter,
} from '@angular/router';
import { vi } from 'vitest';

import { Auth } from '../auth';
import { authGuard } from './auth-guard';

describe('authGuard', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    });
  });

  it('sends a signed-out visitor to sign in with the intended URL', async () => {
    TestBed.inject(Auth).currentUser.set(null);
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);

    const allowed = await TestBed.runInInjectionContext(() =>
      authGuard(
        {} as ActivatedRouteSnapshot,
        { url: '/plan?week=2' } as RouterStateSnapshot,
      ),
    );

    expect(allowed).toBe(false);
    expect(navigate).toHaveBeenCalledWith(['/login'], {
      queryParams: { next: '/plan?week=2' },
    });
  });
});
