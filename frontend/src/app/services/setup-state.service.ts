import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { SetupState } from '../shared/setup-state';

export interface SetupAccount {
  state: string;
  label: string;
  checks: Record<string, boolean>;
  missing: string[];
  last_activity_at: string | null;
  age_days: number | null;
  stale: boolean;
}

export interface SetupStateResponse {
  account: SetupAccount;
  surfaces: Record<string, SetupState>;
}

/**
 * Account-wide setup state for the navigation shell.
 *
 * The navigation stays visible for every account, but a surface that still
 * needs setup is badged so it never implies a capability that has not been
 * configured yet.
 */
@Injectable({ providedIn: 'root' })
export class SetupStateService {
  private readonly http = inject(HttpClient);

  readonly account = signal<SetupAccount | null>(null);
  readonly surfaces = signal<Record<string, SetupState>>({});
  readonly loaded = signal(false);

  load(): void {
    this.http.get<SetupStateResponse>('/api/setup/state').subscribe({
      next: (response) => {
        this.account.set(response?.account ?? null);
        this.surfaces.set(response?.surfaces ?? {});
        this.loaded.set(true);
      },
      error: () => this.loaded.set(true),
    });
  }

  surface(name: string): SetupState | null {
    return this.surfaces()[name] ?? null;
  }

  needsSetup(name: string): boolean {
    const state = this.surface(name);
    return !!state && state.state !== 'ready';
  }
}
