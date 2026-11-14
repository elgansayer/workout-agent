import { CommonModule } from '@angular/common';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Component, OnInit, inject, signal } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { HlmAlertImports } from '@spartan-ng/ui/alert';
import { HlmButtonImports } from '@spartan-ng/ui/button';
import { HlmCardImports } from '@spartan-ng/ui/card';

import { Auth } from '../../services/auth';

/**
 * Public recovery entry point.
 *
 * A message link opens this page with a single-use token. Without a token the
 * page only starts the non-enumerating request flow: the server returns the
 * same accepted answer for every address.
 */
@Component({
  selector: 'app-recovery',
  standalone: true,
  imports: [CommonModule, HlmAlertImports, HlmButtonImports, HlmCardImports],
  templateUrl: './recovery.html',
  styleUrl: './recovery.css',
})
export class Recovery implements OnInit {
  private readonly http = inject(HttpClient);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly auth = inject(Auth);

  protected readonly email = signal('');
  protected readonly token = signal<string | null>(null);
  protected readonly kind = signal<'recovery' | 'email_change'>('recovery');
  protected readonly busy = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly status = signal<string | null>(null);

  ngOnInit(): void {
    const query = this.route.snapshot.queryParamMap;
    const token = query.get('token');
    this.token.set(token && token.length <= 512 ? token : null);
    this.kind.set(query.get('kind') === 'email_change' ? 'email_change' : 'recovery');
  }

  protected updateEmail(value: string): void {
    this.email.set(value);
  }

  protected requestRecovery(): void {
    if (this.busy()) return;
    this.busy.set(true);
    this.error.set(null);
    this.status.set(null);
    this.http.post('/api/auth/recovery/request', { email: this.email().trim() }).subscribe({
      next: () => {
        this.busy.set(false);
        this.status.set(
          'If that address belongs to an account, a recovery link is on its way. Check your inbox.',
        );
      },
      error: () => {
        this.busy.set(false);
        this.status.set(
          'If that address belongs to an account, a recovery link is on its way. Check your inbox.',
        );
      },
    });
  }

  protected confirmToken(): void {
    const token = this.token();
    if (!token || this.busy()) return;
    this.busy.set(true);
    this.error.set(null);
    this.status.set(null);
    const endpoint =
      this.kind() === 'email_change' ? '/api/account/email/confirm' : '/api/auth/recovery/complete';
    this.http.post(endpoint, { token }).subscribe({
      next: () => {
        this.busy.set(false);
        if (this.kind() === 'email_change') {
          this.status.set('Your new e-mail address is verified. Sign in again to continue.');
          this.token.set(null);
          return;
        }
        // The server has issued a new session. Refresh the client identity
        // before entering a guarded route, or the guard would bounce us back.
        void this.auth.checkAuth().then(() => this.router.navigate(['/dashboard']));
      },
      error: (error: unknown) => {
        this.busy.set(false);
        this.error.set(
          error instanceof HttpErrorResponse && typeof error.error?.detail === 'string'
            ? error.error.detail
            : 'This link is invalid or has expired. Request a new one.',
        );
      },
    });
  }
}
