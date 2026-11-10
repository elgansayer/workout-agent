import { Injectable, inject, signal } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { googleSignInHref } from './auth/destination';

export interface User {
  id: string;
  email: string;
  name?: string;
  picture?: string;
}

/**
 * How a newcomer gets an account on this deployment: `open` (the first Google
 * sign-in registers one), `restricted` (an allowlist limits who may sign in) or
 * `invite_only`. It describes the deployment, never a person.
 */
export type RegistrationMode = 'open' | 'restricted' | 'invite_only';

export interface SignInConfig {
  google: boolean;
  registration: RegistrationMode;
}

const REGISTRATION_MODES: ReadonlySet<string> = new Set(['open', 'restricted', 'invite_only']);

/** Accept only the exact shape the server sends; anything else is "unknown". */
export function parseSignInConfig(payload: unknown): SignInConfig | null {
  if (typeof payload !== 'object' || payload === null) return null;
  const { google, registration } = payload as Record<string, unknown>;
  if (typeof google !== 'boolean') return null;
  if (typeof registration !== 'string' || !REGISTRATION_MODES.has(registration)) return null;
  return { google, registration: registration as RegistrationMode };
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

  /**
   * Starts Google sign-in. `next` is the page to come back to afterwards; the
   * server validates it again and ignores anything that is not a page of this
   * application.
   */
  login(next: string | null = null) {
    this.navigate(googleSignInHref(next));
  }

  /**
   * What the sign-in page may know before anyone has signed in. `null` when it
   * could not be read, so the page falls back to generic wording instead of
   * guessing how registration works.
   */
  async loadSignInConfig(): Promise<SignInConfig | null> {
    try {
      return parseSignInConfig(await firstValueFrom(this.http.get<unknown>('/api/auth/config')));
    } catch {
      return null;
    }
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
