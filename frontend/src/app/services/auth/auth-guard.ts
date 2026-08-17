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
  
  window.location.href = '/login/google';
  return false;
};
