import { Injectable, inject, signal } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

export interface User {
  id: string;
  email: string;
  name?: string;
  picture?: string;
  email_verified?: boolean;
}

@Injectable({ providedIn: 'root' })
export class Auth {
  private http = inject(HttpClient);

  // Signal holding the current user state
  currentUser = signal<User | null | undefined>(undefined); // undefined means loading

  async checkAuth() {
    try {
      const res = await firstValueFrom(this.http.get<{user: User}>('/api/me'));
      this.currentUser.set(res.user);
    } catch {
      this.currentUser.set(null);
    }
  }

  login() {
    window.location.href = '/login/google';
  }

  /**
   * Ends this browser's session on the server, then returns to the sign-in page.
   *
   * The server revokes the session, so a cookie that was copied earlier stops
   * working. We therefore only report success once the server has answered. A
   * 401 means the session had already ended. Any other failure (offline, 5xx)
   * falls back to a full-page navigation to the legacy endpoint, which either
   * completes the sign-out or visibly fails in the browser, instead of silently
   * leaving the session alive on a shared device.
   */
  async logout(): Promise<void> {
    try {
      await firstValueFrom(this.http.post('/api/auth/logout', {}));
    } catch (error) {
      if (!(error instanceof HttpErrorResponse && error.status === 401)) {
        this.navigate('/logout');
        return;
      }
    }
    this.finishSignOut();
  }

  /**
   * Revokes every session of this account on every device, including this one.
   * Rejects when the server could not confirm, so the caller can offer a retry.
   */
  async logoutAllDevices(): Promise<void> {
    await firstValueFrom(this.http.post('/api/auth/logout-all', {}));
    this.finishSignOut();
  }

  /** The API reported that this session is no longer valid (expired or revoked). */
  markSessionEnded(): void {
    this.currentUser.set(null);
  }

  private finishSignOut(): void {
    this.currentUser.set(null);
    this.navigate('/login');
  }

  /** Full-page navigation, so no signed-in state survives in memory. */
  protected navigate(url: string): void {
    window.location.assign(url);
  }
}
