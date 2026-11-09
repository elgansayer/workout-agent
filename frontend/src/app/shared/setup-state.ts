/**
 * Shared setup-state contract for surfaces that can legitimately be empty.
 *
 * The backend derives one truthful account state per tenant; every surface
 * renders it so a zero-data view explains why the data is absent and which
 * verified setup action comes next.  The view state keeps loading, empty,
 * stale and failed distinguishable instead of collapsing them into
 * "no data yet".
 */

export type SetupStateKind =
  | 'new'
  | 'partially_configured'
  | 'ready'
  | 'stale'
  | 'disconnected';

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
  action: SetupAction | null;
  missing: string[];
  last_activity_at: string | null;
  age_days: number | null;
  stale: boolean;
}

export type ViewState = 'loading' | 'ready' | 'empty' | 'stale' | 'failed';

export interface ViewStateInput {
  loading: boolean;
  error: string | null;
  hasData: boolean;
  stale?: boolean;
}

/** Resolve the state of a surface from its request and payload. */
export function resolveViewState(input: ViewStateInput): ViewState {
  if (input.error) return 'failed';
  if (!input.hasData) return input.loading ? 'loading' : 'empty';
  if (input.stale) return 'stale';
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
