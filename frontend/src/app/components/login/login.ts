import { HlmCardImports } from '@spartan-ng/ui/card';
import { HlmButtonImports } from '@spartan-ng/ui/button';
import { HlmAlertImports } from '@spartan-ng/ui/alert';
import { Component, inject, signal } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { Auth } from '../../services/auth';

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
  protected readonly googleLoginHref: string;
  private route = inject(ActivatedRoute);

  constructor() {
    const code = this.route.snapshot.queryParamMap.get('error');
    this.error.set(code === null ? null : (LOGIN_ERROR_MESSAGES.get(code) ?? null));
    // The server validates the destination again before storing it, so this is
    // only transport: carrying it here lets a deep link survive the OAuth trip.
    const next = this.route.snapshot.queryParamMap.get('next');
    this.googleLoginHref = next
      ? `/login/google?next=${encodeURIComponent(next)}`
      : '/login/google';
  }
}
