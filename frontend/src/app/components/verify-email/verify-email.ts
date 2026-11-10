import { CommonModule } from '@angular/common';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Component, OnInit, inject, signal } from '@angular/core';

@Component({
  selector: 'app-verify-email',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './verify-email.html',
  styleUrl: './verify-email.css',
})
export class VerifyEmail implements OnInit {
  private http = inject(HttpClient);

  token = '';
  submitting = signal(false);
  message = signal('');
  error = signal<string | null>(null);
  verified = signal(false);

  ngOnInit(): void {
    const match = /(?:^#|&)token=([^&]+)/.exec(window.location.hash);
    if (match) {
      this.token = decodeURIComponent(match[1]);
    }
  }

  confirm(): void {
    if (!this.token) {
      this.error.set('This verification link is missing its token.');
      return;
    }
    this.submitting.set(true);
    this.error.set(null);
    this.http
      .post<{ status: string; email: string; previous_address_alerted: boolean }>(
        '/api/account/email/confirm',
        { token: this.token },
      )
      .subscribe({
        next: (response) => {
          this.submitting.set(false);
          this.verified.set(true);
          this.message.set(`Your account email is now ${response.email}.`);
          if (!response.previous_address_alerted) {
            this.message.set(
              `Your account email is now ${response.email}. The security notice to your previous address could not be delivered.`,
            );
          }
        },
        error: (err: HttpErrorResponse) => {
          this.submitting.set(false);
          this.error.set(
            err.error?.detail || 'This verification link is invalid or has expired.',
          );
        },
      });
  }
}
