import {
  HttpBackend,
  HttpClient,
  HttpErrorResponse,
  HttpInterceptorFn,
} from '@angular/common/http';
import { inject } from '@angular/core';
import { Router } from '@angular/router';
import { catchError, of, switchMap, throwError } from 'rxjs';

import { environment } from '../../environments/environment';
import { Auth } from '../services/auth';

interface CsrfTokenResponse {
  token: string;
  expires_in: number;
}

const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

// These account-recovery mutations are intentionally reachable before a
// session exists. When a session does exist we still attach a CSRF token, but
// an anonymous token lookup must not prevent the recovery request itself.
const PUBLIC_MUTATION_PATHS = new Set([
  '/api/account/recovery/request',
  '/api/account/recovery/complete',
  '/api/account/email/confirm',
]);

function requestPath(url: string, baseUrl: string): string {
  let path = url.startsWith(baseUrl) && baseUrl ? url.slice(baseUrl.length) : url;
  const queryIndex = path.indexOf('?');
  if (queryIndex !== -1) {
    path = path.slice(0, queryIndex);
  }
  return path;
}

// The auth probe is expected to answer 401 for a signed-out visitor, so that
// answer must not bounce the visitor away from where they already are.
function isAuthProbe(url: string): boolean {
  return url.split('?')[0].endsWith('/api/me');
}

export const apiInterceptor: HttpInterceptorFn = (req, next) => {
  const auth = inject(Auth);
  const router = inject(Router);
  const baseUrl = environment.apiUrl ? environment.apiUrl.replace(/\/$/, '') : '';
  let url = req.url;

  if (url.startsWith('/api') && baseUrl) {
    url = `${baseUrl}${url}`;
  }

  const modifiedReq = req.clone({
    url,
    withCredentials: true,
  });

  const relativeBackendRequest = req.url.startsWith('/');
  const absoluteBackendRequest = Boolean(baseUrl) && req.url.startsWith(baseUrl);
  const isBackendRequest = relativeBackendRequest || absoluteBackendRequest;
  const requiresCsrf = UNSAFE_METHODS.has(req.method.toUpperCase()) && isBackendRequest;

  // The server ends sessions (idle or absolute limit, sign-out on another
  // device, access withdrawn) and answers 401 from then on. Return to sign-in
  // instead of leaving a page full of failed requests.
  const handleError = (error: unknown) => {
    if (
      error instanceof HttpErrorResponse &&
      error.status === 401 &&
      isBackendRequest &&
      !isAuthProbe(req.url)
    ) {
      auth.markSessionEnded();
      if (!router.url.startsWith('/login')) {
        void router.navigate(['/login']);
      }
    }
    return throwError(() => error);
  };

  if (!requiresCsrf) {
    return next(modifiedReq).pipe(catchError(handleError));
  }

  // Each browser mutation uses a fresh, server-side single-use nonce. Use the
  // raw backend client so the token lookup cannot recursively re-enter this
  // interceptor; the request URL and credentials are explicit here instead.
  const csrfClient = new HttpClient(inject(HttpBackend));
  const csrfUrl = baseUrl ? `${baseUrl}/api/csrf-token` : '/api/csrf-token';
  const publicMutation = PUBLIC_MUTATION_PATHS.has(requestPath(req.url, baseUrl));
  return csrfClient
    .get<CsrfTokenResponse>(csrfUrl, { withCredentials: true })
    .pipe(
      catchError((error: unknown) => {
        if (!publicMutation) {
          return throwError(() => error);
        }
        return of(null);
      }),
      switchMap((response) =>
        next(
          response
            ? modifiedReq.clone({
                setHeaders: { 'X-CSRF-Token': response.token },
              })
            : modifiedReq,
        ),
      ),
      catchError(handleError),
    );
};
