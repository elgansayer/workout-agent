/**
 * Shared readiness contract for surfaces that can legitimately be empty.
 *
 * The backend derives one truthful readiness state per tenant from that
 * tenant's own records; every surface renders it so a zero-data or partial view
 * explains why, and which verified setup action comes next.  The six states are
 * `setup_required`, `connecting`, `syncing`, `ready`, `stale` and `degraded`.
 * The view state keeps loading, empty, attention and failed distinguishable
 * instead of collapsing them into "no data yet".
 */

export type SetupStateKind =
  'setup_required' | 'connecting' | 'syncing' | 'ready' | 'stale' | 'degraded';

export interface SetupAction {
  id: string;
  label: string;
  href: string;
  description: string;
}

export interface SetupState {
  state: SetupStateKind;
  label: string;
  reason: string;
  /** Machine-readable cause, e.g. `credential_rejected` or `sync_failed`. */
  reason_code?: string | null;
  /** True while nothing at all is configured yet (a brand-new account). */
  first_run?: boolean;
  action: SetupAction | null;
  missing: string[];
  last_activity_at: string | null;
  age_days: number | null;
  stale: boolean;
}

/**
 * `attention` means data is shown, but a notice must accompany it: the data is
 * being rebuilt, is out of date, or its source needs repair.
 */
export type ViewState = 'loading' | 'ready' | 'empty' | 'attention' | 'failed';

export interface ViewStateInput {
  loading: boolean;
  error: string | null;
  hasData: boolean;
  attention?: boolean;
}

/** Resolve the state of a surface from its request and payload. */
export function resolveViewState(input: ViewStateInput): ViewState {
  if (input.error) return 'failed';
  if (!input.hasData) return input.loading ? 'loading' : 'empty';
  if (input.attention) return 'attention';
  return 'ready';
}

/** Return the next setup action, or null when nothing needs doing. */
export function setupAction(setup: SetupState | null | undefined): SetupAction | null {
  if (!setup || setup.state === 'ready') return null;
  return setup.action ?? null;
}

/** Whether a setup state still blocks the surface from showing real data. */
export function needsSetup(setup: SetupState | null | undefined): boolean {
  return !!setup && setup.state !== 'ready';
}

/** Whether data is shown but a notice must accompany it. */
export function needsAttention(setup: SetupState | null | undefined): boolean {
  return !!setup && setup.state !== 'ready';
}

/** Whether a connect or sync is running, so the data may still be changing. */
export function isLive(setup: SetupState | null | undefined): boolean {
  return setup?.state === 'connecting' || setup?.state === 'syncing';
}

export interface NavBadge {
  label: string;
  kind: SetupStateKind;
  /** Spoken suffix for the link's accessible name. */
  spoken: string;
}

/** The short badge a navigation link shows for a surface's state, if any. */
export function navBadge(setup: Pick<SetupState, 'state'> | null | undefined): NavBadge | null {
  switch (setup?.state) {
    case 'setup_required':
      return { label: 'Setup', kind: 'setup_required', spoken: 'setup needed' };
    case 'connecting':
      return { label: 'Connecting', kind: 'connecting', spoken: 'connecting to Hevy' };
    case 'syncing':
      return { label: 'Syncing', kind: 'syncing', spoken: 'syncing' };
    case 'stale':
      return { label: 'Stale', kind: 'stale', spoken: 'data out of date' };
    case 'degraded':
      return { label: 'Attention', kind: 'degraded', spoken: 'needs attention' };
    default:
      return null;
  }
}
