import { Component, OnInit, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { Auth } from '../../services/auth';

interface ActiveSession {
  id: number;
  device: string;
  location: string;
  created_at: string;
  last_used_at: string;
  current: boolean;
}

@Component({
  selector: 'app-user-profile',
  imports: [CommonModule],
  templateUrl: './user-profile.html',
  styleUrl: './user-profile.css',
})
export class UserProfile implements OnInit {
  authService = inject(Auth);
  private http = inject(HttpClient);

  signingOutEverywhere = signal(false);
  error = signal<string | null>(null);

  newEmail = signal('');
  emailBusy = signal(false);
  emailError = signal<string | null>(null);
  emailStatus = signal<string | null>(null);

  sessions = signal<ActiveSession[]>([]);
  sessionsLoading = signal<boolean>(true);
  sessionsError = signal<string | null>(null);
  sessionStatus = signal<string>('');

  ngOnInit() {
    this.loadSessions();
  }

  loadSessions() {
    this.sessionsError.set(null);
    this.http.get<{ sessions: ActiveSession[] }>('/api/sessions').subscribe({
      next: (response) => {
        this.sessions.set(response.sessions || []);
        this.sessionsLoading.set(false);
      },
      error: () => {
        this.sessionsError.set('Could not load active sessions.');
        this.sessionsLoading.set(false);
      },
    });
  }

  revokeSession(session: ActiveSession) {
    const question = session.current
      ? 'Sign out of this session?'
      : `Revoke the session on ${session.device}?`;
    if (!confirm(question)) return;

    this.sessionStatus.set('');
    this.http.delete<{ logged_out?: boolean }>(`/api/sessions/${session.id}`).subscribe({
      next: (response) => {
        if (response.logged_out) {
          window.location.assign('/login');
          return;
        }
        this.sessionStatus.set('Session revoked. Your current session was refreshed.');
        this.loadSessions();
      },
      error: () => this.sessionStatus.set('Could not revoke that session. Try again.'),
    });
  }

  updateNewEmail(value: string): void {
    this.newEmail.set(value);
  }

  requestEmailChange(): void {
    if (this.emailBusy()) return;
    this.emailBusy.set(true);
    this.emailError.set(null);
    this.emailStatus.set(null);
    this.http
      .post<{ message?: string }>('/api/account/email/change-request', {
        new_email: this.newEmail().trim(),
      })
      .subscribe({
        next: (response) => {
          this.emailBusy.set(false);
          this.emailStatus.set(
            response.message ?? 'Check the new address for a verification link.',
          );
          this.newEmail.set('');
        },
        error: (error) => {
          this.emailBusy.set(false);
          const detail = error?.error?.detail;
          this.emailError.set(
            typeof detail === 'string' ? detail : 'Could not start the e-mail change. Try again.',
          );
        },
      });
  }

  revokeOtherSessions() {
    if (!confirm('Revoke all other sessions? Every other device will be signed out.')) return;

    this.sessionStatus.set('');
    this.http.post<{ revoked: number }>('/api/sessions/revoke-others', {}).subscribe({
      next: (response) => {
        const count = response.revoked || 0;
        this.sessionStatus.set(
          count === 0
            ? 'No other active sessions to revoke.'
            : `Revoked ${count} other session${count === 1 ? '' : 's'}.`,
        );
        this.loadSessions();
      },
      error: () => this.sessionStatus.set('Could not revoke other sessions. Try again.'),
    });
  }

  logout() {
    void this.authService.logout();
  }

  /** Revoke every session of this account, on every device, after confirmation. */
  async logoutAllDevices(): Promise<void> {
    if (
      !confirm(
        'Sign out of every device and browser? You will need to sign in again everywhere, including here.',
      )
    ) {
      return;
    }
    this.error.set(null);
    this.signingOutEverywhere.set(true);
    try {
      // On success the page navigates to sign-in, so the busy state is kept.
      await this.authService.logoutAllDevices();
    } catch {
      this.error.set('We could not sign you out of all devices. Please try again.');
      this.signingOutEverywhere.set(false);
    }
  }
}
