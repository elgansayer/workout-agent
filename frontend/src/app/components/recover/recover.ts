import { CommonModule } from '@angular/common';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

@Component({
  selector: 'app-recover',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './recover.html',
  styleUrl: './recover.css',
})
export class Recover implements OnInit {
  private http = inject(HttpClient);

  email = '';
  token = '';
  submitting = signal(false);
  message = signal('');
  error = signal<string | null>(null);
  complete = signal(false);

  ngOnInit(): void {
    const match = /(?:^#|&)token=([^&]+)/.exec(window.location.hash);
    if (match) {
      this.token = decodeURIComponent(match[1]);
    }
  }

  requestRecovery(): void {
    const email = this.email.trim();
    if (!email) {
      this.error.set('Enter your account email address.');
      return;
    }
    this.submitting.set(true);
    this.error.set(null);
    this.http.post('/api/account/recovery/request', { email }).subscribe({
      next: () => {
        this.submitting.set(false);
        this.message.set(
          'If an active account with a verified address exists, a single-use recovery link has been sent. It expires in 15 minutes.',
        );
      },
      error: () => {
        this.submitting.set(false);
        this.message.set(
          'If an active account with a verified address exists, a single-use recovery link has been sent. It expires in 15 minutes.',
        );
      },
    });
  }

  completeRecovery(): void {
    if (!this.token) {
      this.error.set('This recovery link is missing its token.');
      return;
    }
    this.submitting.set(true);
    this.error.set(null);
    this.http
      .post<{ status: string }>('/api/account/recovery/complete', { token: this.token })
      .subscribe({
        next: () => {
          this.submitting.set(false);
          this.complete.set(true);
          window.location.assign('/dashboard');
        },
        error: (err: HttpErrorResponse) => {
          this.submitting.set(false);
          this.error.set(
            err.error?.detail || 'This recovery link is invalid or has expired.',
          );
        },
      });
  }
}
