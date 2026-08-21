import { HttpBackend, HttpClient, HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { switchMap } from 'rxjs';

import { environment } from '../../environments/environment';

interface CsrfTokenResponse {
  token: string;
  expires_in: number;
}

const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

export const apiInterceptor: HttpInterceptorFn = (req, next) => {
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
  const requiresCsrf =
    UNSAFE_METHODS.has(req.method.toUpperCase()) &&
    (relativeBackendRequest || absoluteBackendRequest);

  if (!requiresCsrf) {
    return next(modifiedReq);
  }

  // Each browser mutation uses a fresh, server-side single-use nonce. Use the
  // raw backend client so the token lookup cannot recursively re-enter this
  // interceptor; the request URL and credentials are explicit here instead.
  const csrfClient = new HttpClient(inject(HttpBackend));
  const csrfUrl = baseUrl ? `${baseUrl}/api/csrf-token` : '/api/csrf-token';
  return csrfClient
    .get<CsrfTokenResponse>(csrfUrl, { withCredentials: true })
    .pipe(
      switchMap(({ token }) =>
        next(
          modifiedReq.clone({
            setHeaders: { 'X-CSRF-Token': token },
          }),
        ),
      ),
    );
};
