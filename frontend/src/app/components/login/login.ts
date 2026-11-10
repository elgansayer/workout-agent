import { HlmCardImports } from '@spartan-ng/ui/card';
import { HlmButtonImports } from '@spartan-ng/ui/button';
import { HlmAlertImports } from '@spartan-ng/ui/alert';
import { Component, computed, inject, signal } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { Auth, RegistrationMode } from '../../services/auth';
import { googleSignInHref, resumeDestination } from '../../services/auth/destination';

/**
 * Fixed copy for the two error codes the server can redirect here with.
 *
 * The server deliberately uses one code for every refusal (not allowlisted, not
 * invited, suspended, deleted, unverified email), so this page cannot tell a
 * person which rule applied. The query string is attacker-controllable, so only
 * an exact known code selects a message; anything else shows nothing and is
 * never echoed. A Map avoids prototype-chain lookups for keys like "constructor".
 */
const LOGIN_ERROR_MESSAGES: ReadonlyMap<string, string> = new Map([
  [
    'access_denied',
    "This account can't be used to sign in here. If you expected access, contact the person who runs this dashboard.",
  ],
  ['unavailable', 'Sign-in is temporarily unavailable. Please try again in a moment.'],
]);

/**
 * How this deployment admits a newcomer, in words. It describes the
 * deployment, never a person, so it says nothing about which addresses,
 * domains or invitations exist. Only an exact known mode selects a sentence.
 */
const REGISTRATION_NOTES: ReadonlyMap<RegistrationMode, string> = new Map([
  ['open', 'New here? Signing in with Google creates your account.'],
  ['restricted', 'Access is limited to approved Google accounts.'],
  [
    'invite_only',
    'Registration is by invitation. Sign in with the Google account your invitation was sent to.',
  ],
]);

@Component({
  selector: 'app-login',
  imports: [HlmCardImports, HlmButtonImports, HlmAlertImports],
  templateUrl: './login.html',
  styleUrl: './login.css'
})
export class Login {
  loading = signal<boolean>(false);
  error = signal<string|null>(null);
  data = signal<any>({});

  protected auth = inject(Auth);
  private route = inject(ActivatedRoute);

  /** The page to resume at after signing in (a same-origin path, or nothing). */
  private readonly next = signal<string | null>(null);
  /** Unknown (null) until the server answers, or if it cannot be read. */
  protected readonly registration = signal<RegistrationMode | null>(null);
  /** Assume Google sign-in works until the server says it is not configured. */
  protected readonly googleConfigured = signal(true);

  protected readonly signInHref = computed(() => googleSignInHref(this.next()));
  protected readonly registrationNote = computed(() => {
    const mode = this.registration();
    return mode === null ? null : (REGISTRATION_NOTES.get(mode) ?? null);
  });

  constructor() {
    const params = this.route.snapshot.queryParamMap;
    const code = params.get('error');
    this.error.set(code === null ? null : (LOGIN_ERROR_MESSAGES.get(code) ?? null));
    this.next.set(resumeDestination(params.get('next')));
    void this.loadConfig();
  }

  private async loadConfig(): Promise<void> {
    const config = await this.auth.loadSignInConfig();
    if (config === null) return;
    this.registration.set(config.registration);
    this.googleConfigured.set(config.google);
  }
}
