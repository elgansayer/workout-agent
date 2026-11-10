/**
 * Where a person returns to after signing in.
 *
 * Mirrors `backend/webapp/login_destination.py`, which is the authority: the
 * server validates again before it redirects. This copy exists so the sign-in
 * page never builds a link the server would drop, and so a refused page can say
 * where the visitor was headed.
 *
 * A destination is a same-origin page path (with an optional query and
 * fragment), never a URL: a scheme, host, `//` prefix, backslash, control
 * character, dot segment or non-ASCII character makes it invalid. Sign-in
 * machinery and APIs are never a destination. An invalid destination is not an
 * error; the visitor simply lands on the default page.
 */

const MAX_DESTINATION_LENGTH = 512;

/** The default landing page; adding it to a sign-in link would say nothing. */
const DEFAULT_DESTINATION = '/';

const EXCLUDED_PREFIXES = [
  '/login',
  '/logout',
  '/auth',
  '/api',
  '/static',
  '/assets',
  '/google-health',
  '/livez',
  '/readyz',
];
const EXCLUDED_PATHS = new Set(['/sw.js', '/favicon.ico']);

// Printable ASCII without whitespace: rejects control characters, spaces and
// anything non-ASCII in one test.
const PRINTABLE_ASCII = /^[\x21-\x7e]+$/;
const CONTROL_CHARACTER = /[\x00-\x1f\x7f]/;

/** Return `raw` if it is a safe same-origin destination, else `null`. */
export function safeDestination(raw: unknown): string | null {
  if (typeof raw !== 'string' || raw.length === 0 || raw.length > MAX_DESTINATION_LENGTH) {
    return null;
  }
  if (!PRINTABLE_ASCII.test(raw) || !raw.startsWith('/') || raw.startsWith('//')) return null;
  if (raw.includes('\\')) return null;

  const end = raw.search(/[?#]/);
  const rawPath = end === -1 ? raw : raw.slice(0, end);
  let path: string;
  try {
    // Percent-encoding must not smuggle in what the raw check refused.
    path = decodeURIComponent(rawPath);
  } catch {
    return null;
  }
  if (!path.startsWith('/') || path.startsWith('//')) return null;
  if (path.includes('\\') || CONTROL_CHARACTER.test(path)) return null;
  if (path.split('/').some((segment) => segment === '.' || segment === '..')) return null;

  const lowered = path.toLowerCase();
  if (
    EXCLUDED_PATHS.has(lowered) ||
    EXCLUDED_PREFIXES.some((prefix) => lowered === prefix || lowered.startsWith(`${prefix}/`))
  ) {
    return null;
  }
  return raw;
}

/**
 * The destination worth remembering for `url`: valid, and not the default page.
 * Use this wherever a redirect to sign-in should be able to resume.
 */
export function resumeDestination(url: unknown): string | null {
  const destination = safeDestination(url);
  return destination !== null && destination !== DEFAULT_DESTINATION ? destination : null;
}

/** Query parameters for the sign-in page that resume at `url` afterwards. */
export function signInQueryParams(url: unknown): { next?: string } {
  const next = resumeDestination(url);
  return next === null ? {} : { next };
}

/** The link that starts Google sign-in and returns to `next` afterwards. */
export function googleSignInHref(next: string | null): string {
  const destination = resumeDestination(next);
  return destination === null
    ? '/login/google'
    : `/login/google?next=${encodeURIComponent(destination)}`;
}
