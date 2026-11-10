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

  logout() {
    this.authService.logout();
  }
}
