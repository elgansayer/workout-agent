import {
  googleSignInHref,
  resumeDestination,
  safeDestination,
  signInQueryParams,
} from './destination';

describe('safeDestination', () => {
  it.each([
    '/',
    '/plan',
    '/plan?week=2',
    '/settings#hevy',
    '/programmes?goal=strength&weeks=8#preview',
    '/history/2026-11-05',
    '/plan%20b', // an encoded space is data, not whitespace
    '/loginx', // shares a prefix with /login but is a different page
    '/authors',
    '/api-docs',
  ])('keeps the same-origin page path %s unchanged', (path) => {
    expect(safeDestination(path)).toBe(path);
  });

  it.each([
    undefined,
    null,
    '',
    0,
    ['/plan'],
    { next: '/plan' },
    'plan',
    '//evil.example',
    '//evil.example/plan',
    '///evil.example',
    '/\\evil.example',
    '\\\\evil.example',
    '/plan\\..\\evil',
    'https://evil.example/plan',
    'http://evil.example',
    'javascript:alert(1)',
    'data:text/html,x',
    '/%2F/evil.example',
    '/%2f/evil.example',
    '/%5Cevil.example',
    '/plan\r\nSet-Cookie: x=1',
    '/plan%0d%0aSet-Cookie:x=1',
    '/plan%00',
    '/plan\u0000',
    '/plan\tb',
    '/plan b',
    '/plan b',
    '/pläne',
    '/../etc/passwd',
    '/plan/../../etc',
    '/plan/./x',
    '/%2e%2e/secret',
    '/plan%zz', // malformed percent-encoding is never interpreted
    `/${'a'.repeat(512)}`,
  ])('drops anything that could leave the site or inject a header: %j', (raw) => {
    expect(safeDestination(raw)).toBeNull();
  });

  it.each([
    '/login',
    '/login/google',
    '/login?error=access_denied',
    '/Login',
    '/%6Cogin',
    '/logout',
    '/auth',
    '/auth?code=1',
    '/api',
    '/api/me',
    '/api%2Fme',
    '/API/settings',
    '/static/app.css',
    '/assets/main.js',
    '/google-health/connect',
    '/livez',
    '/readyz',
    '/sw.js',
    '/favicon.ico',
  ])('never offers the sign-in machinery or an API as a destination: %s', (path) => {
    expect(safeDestination(path)).toBeNull();
  });
});

describe('resumeDestination', () => {
  it('remembers a real page', () => {
    expect(resumeDestination('/plan?week=2')).toBe('/plan?week=2');
  });

  it('treats the default page as nothing to remember', () => {
    expect(resumeDestination('/')).toBeNull();
  });

  it('forgets anything unsafe', () => {
    expect(resumeDestination('//evil.example')).toBeNull();
    expect(resumeDestination('/login')).toBeNull();
  });
});

describe('signInQueryParams', () => {
  it('carries the page to resume at', () => {
    expect(signInQueryParams('/plan?week=2')).toEqual({ next: '/plan?week=2' });
  });

  it('carries nothing for the default page or an unsafe target', () => {
    expect(signInQueryParams('/')).toEqual({});
    expect(signInQueryParams('//evil.example')).toEqual({});
    expect(signInQueryParams(undefined)).toEqual({});
  });
});

describe('googleSignInHref', () => {
  it('is the plain sign-in link without a destination', () => {
    expect(googleSignInHref(null)).toBe('/login/google');
    expect(googleSignInHref('/')).toBe('/login/google');
  });

  it('encodes the destination as one query value', () => {
    expect(googleSignInHref('/plan?week=2&x=1#top')).toBe(
      '/login/google?next=%2Fplan%3Fweek%3D2%26x%3D1%23top',
    );
  });

  it('never builds a link to an unsafe destination', () => {
    for (const hostile of ['//evil.example', 'https://evil.example', '/login', '/api/me']) {
      expect(googleSignInHref(hostile)).toBe('/login/google');
    }
  });
});
