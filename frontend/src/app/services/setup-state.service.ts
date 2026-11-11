import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { NavBadge, SetupState, SetupStateKind, navBadge } from '../shared/setup-state';

export interface SetupConnectorSync {
  outcome: 'succeeded' | 'failed';
  at: string | null;
  /** A fixed failure code, never provider text. */
  code: string | null;
  records: number | null;
}

export interface SetupConnector {
  provider: string;
  operation: 'connecting' | 'syncing' | null;
  operation_started_at: string | null;
  operation_stalled: boolean;
  last_sync: SetupConnectorSync | null;
}

export interface SetupAccount {
  state: SetupStateKind;
  label: string;
  reason: string;
  reason_code: string | null;
  first_run: boolean;
  checks: Record<string, boolean>;
  missing: string[];
  last_activity_at: string | null;
  age_days: number | null;
  stale: boolean;
  connection_state: string | null;
  connector: SetupConnector | null;
  ai_provider_state: string | null;
  /** `user` for the account's own key, `server` for the shared fallback key. */
  ai_provider_source?: string | null;
}

export interface SetupStateResponse {
  account: SetupAccount;
  surfaces: Record<string, SetupState>;
}

/**
 * Account-wide readiness for the navigation shell and the Settings page.
 *
 * The navigation stays visible for every account, but a surface whose data is
 * missing, being rebuilt, out of date or failing is badged so it never implies
 * a capability that is not actually working.
 */
@Injectable({ providedIn: 'root' })
export class SetupStateService {
  private readonly http = inject(HttpClient);

  readonly account = signal<SetupAccount | null>(null);
  readonly surfaces = signal<Record<string, SetupState>>({});
  readonly loaded = signal(false);
  /** Set when the last refresh failed, so a stale snapshot is never shown as current. */
  readonly failed = signal(false);

  load(): void {
    this.http.get<SetupStateResponse>('/api/setup/state').subscribe({
      next: (response) => {
        this.account.set(response?.account ?? null);
        this.surfaces.set(response?.surfaces ?? {});
        this.failed.set(false);
        this.loaded.set(true);
      },
      error: () => {
        this.failed.set(true);
        this.loaded.set(true);
      },
    });
  }

  surface(name: string): SetupState | null {
    return this.surfaces()[name] ?? null;
  }

  needsSetup(name: string): boolean {
    const state = this.surface(name);
    return !!state && state.state !== 'ready';
  }

  badge(name: string): NavBadge | null {
    return navBadge(this.surface(name));
  }

  /** The badge for Settings, where connections are fixed, from the account-level state. */
  settingsBadge(): NavBadge | null {
    return navBadge(this.account());
  }

  /** The accessible name of a navigation link, including its state when it has one. */
  navLabel(name: string, base: string): string | null {
    const badge = this.badge(name);
    return badge ? `${base} (${badge.spoken})` : null;
  }
}
