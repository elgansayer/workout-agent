import { HttpInterceptorFn } from '@angular/common/http';
import { environment } from '../../environments/environment';

export const apiInterceptor: HttpInterceptorFn = (req, next) => {
  const baseUrl = environment.apiUrl ? environment.apiUrl.replace(/\/$/, '') : '';
  let url = req.url;

  if (url.startsWith('/api') && baseUrl) {
    url = `${baseUrl}${url}`;
  }

  // Always send session cookies across origins or same-origin
  const modifiedReq = req.clone({
    url,
    withCredentials: true,
  });

  return next(modifiedReq);
};
