import { HttpErrorResponse } from '@angular/common/http';

/**
 * Prefer the server's quota message over a generic fallback.
 *
 * A refused quota request carries an actionable `detail` (which limit applies
 * and when to try again) and an `error` code starting with `quota_`. Other
 * failures keep the caller's own wording.
 */
export function quotaAwareMessage(error: unknown, fallback: string): string {
  if (error instanceof HttpErrorResponse) {
    const body = error.error;
    if (
      typeof body?.error === 'string' &&
      body.error.startsWith('quota_') &&
      typeof body.detail === 'string' &&
      body.detail.trim()
    ) {
      return body.detail;
    }
  }
  return fallback;
}
