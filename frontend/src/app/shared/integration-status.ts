/**
 * Mirrors `IntegrationStatus.to_payload()` / `AccountStatus.to_payload()` from
 * `backend/integration_state.py`. Every surface renders this one model; see
 * `docs/INTEGRATION_STATE_MACHINE.md`.
 */
export type IntegrationState =
  'setup_required' | 'connecting' | 'syncing' | 'ready' | 'stale' | 'degraded';

export interface IntegrationStatus {
  connector: string;
  label: string;
  state: IntegrationState;
  reason: string;
  message: string;
  next_action: string;
  action_label: string | null;
  action_href: string | null;
  retryable: boolean;
  freshness: string | null;
  last_attempt_at: string | null;
  last_success_at: string | null;
  last_error_code: string | null;
  consecutive_failures: number;
  last_run: { fetched: number; written: number; skipped: number } | null;
  required?: boolean;
}

export interface AccountStatus {
  generated_at: string;
  state: IntegrationState;
  reason: string;
  message: string;
  next_action: string;
  action_label: string | null;
  action_href: string | null;
  required: string[];
  blocking: string[];
  components: Record<string, IntegrationStatus>;
}

export const STATE_LABELS: Record<IntegrationState, string> = {
  setup_required: 'Setup required',
  connecting: 'Connecting',
  syncing: 'Syncing',
  ready: 'Ready',
  stale: 'Stale',
  degraded: 'Degraded',
};

export function stateLabel(state: string | null | undefined): string {
  return state && state in STATE_LABELS ? STATE_LABELS[state as IntegrationState] : '';
}

/** States that need a person (or the system) to act before the data can be trusted. */
export function needsAttention(state: string | null | undefined): boolean {
  return state === 'setup_required' || state === 'degraded';
}
