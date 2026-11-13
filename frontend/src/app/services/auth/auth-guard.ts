import { CanActivateFn, Router } from '@angular/router';
import { inject } from '@angular/core';
import { Auth } from '../auth';

export const authGuard: CanActivateFn = async (route, state) => {
  const authService = inject(Auth);
  
  if (authService.currentUser() === undefined) {
    await authService.checkAuth();
  }

  if (authService.currentUser()) {
    return true;
  }
  
  const router = inject(Router);
  void router.navigate(['/login'], { queryParams: { next: state.url } });
  return false;
};
