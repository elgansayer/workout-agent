import { HttpErrorResponse } from '@angular/common/http';
import { quotaAwareMessage } from './api-errors';

function failure(status: number, body: unknown): HttpErrorResponse {
  return new HttpErrorResponse({ status, error: body });
}

describe('quotaAwareMessage', () => {
  it('shows the server message when a quota refuses the request', () => {
    const detail = 'AI requests are limited to 15 per minute. Try again in 42 seconds.';
    expect(quotaAwareMessage(failure(429, { detail, error: 'quota_exceeded' }), 'Failed')).toBe(
      detail,
    );
  });

  it('also explains a switched-off quota and an unavailable limit check', () => {
    const off = 'Reports are turned off for this account. Contact the administrator.';
    const unavailable = 'Usage limits are temporarily unavailable. Try again shortly.';
    expect(quotaAwareMessage(failure(403, { detail: off, error: 'quota_disabled' }), 'x')).toBe(
      off,
    );
    expect(
      quotaAwareMessage(failure(503, { detail: unavailable, error: 'quota_unavailable' }), 'x'),
    ).toBe(unavailable);
  });

  it('keeps the caller wording for every other failure', () => {
    expect(quotaAwareMessage(failure(500, { detail: 'boom' }), 'Fallback')).toBe('Fallback');
    expect(quotaAwareMessage(failure(403, { detail: 'CSRF', error: 'csrf' }), 'Fallback')).toBe(
      'Fallback',
    );
    expect(quotaAwareMessage(failure(429, 'Too Many Requests'), 'Fallback')).toBe('Fallback');
    expect(quotaAwareMessage(failure(429, null), 'Fallback')).toBe('Fallback');
    expect(
      quotaAwareMessage(failure(429, { detail: ' ', error: 'quota_exceeded' }), 'Fallback'),
    ).toBe('Fallback');
    expect(quotaAwareMessage(new Error('network'), 'Fallback')).toBe('Fallback');
  });
});
