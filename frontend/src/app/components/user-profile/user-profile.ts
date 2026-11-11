import { CommonModule } from '@angular/common';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { Auth } from '../../services/auth';

@Component({
  selector: 'app-user-profile',
  imports: [CommonModule, FormsModule],
  templateUrl: './user-profile.html',
  styleUrl: './user-profile.css'
})
export class UserProfile {
  authService = inject(Auth);
  private http = inject(HttpClient);

  newEmail = '';
  emailBusy = signal(false);
  emailStatus = signal('');

  requestEmailChange() {
    const email = this.newEmail.trim();
    if (!email) {
      this.emailStatus.set('Enter the new email address.');
      return;
    }
    this.emailBusy.set(true);
    this.emailStatus.set('');
    this.http.post('/api/account/email/request', { email }).subscribe({
      next: () => {
        this.emailBusy.set(false);
        this.newEmail = '';
        this.emailStatus.set(
          'Verification sent to the new address. It expires in 30 minutes. A security notice was also sent to your current address.',
        );
      },
      error: (err: HttpErrorResponse) => {
        this.emailBusy.set(false);
        this.emailStatus.set(
          err.error?.detail || 'Could not start the email change. Try again.',
        );
      },
    });
  }

  signingOutEverywhere = signal(false);
  error = signal<string | null>(null);

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
