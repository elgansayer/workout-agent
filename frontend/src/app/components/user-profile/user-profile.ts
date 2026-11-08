import { Component, inject, signal } from '@angular/core';
import { Auth } from '../../services/auth';
import { CommonModule } from '@angular/common';

@Component({
  selector: 'app-user-profile',
  imports: [CommonModule],
  templateUrl: './user-profile.html',
  styleUrl: './user-profile.css'
})
export class UserProfile {
  authService = inject(Auth);

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
