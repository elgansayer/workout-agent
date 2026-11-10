import { CanActivateFn, Router } from '@angular/router';
import { inject } from '@angular/core';
import { Auth } from '../auth';
import { signInQueryParams } from './destination';

export const authGuard: CanActivateFn = async (_route, state) => {
  const authService = inject(Auth);
  // Injection is only possible synchronously, so resolve the router before the
  // first await.
  const router = inject(Router);

  if (authService.currentUser() === undefined) {
    await authService.checkAuth();
  }

  if (authService.currentUser()) {
    return true;
  }

  // Send the visitor to sign in, and bring them back to this page afterwards.
  return router.createUrlTree(['/login'], { queryParams: signInQueryParams(state.url) });
};
