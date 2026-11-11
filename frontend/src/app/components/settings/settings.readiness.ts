import { SetupAccount } from '../../services/setup-state.service';

/**
 * Plain-language facts for the Settings readiness panel.
 *
 * Every sentence is derived from the account state the server computed for the
 * signed-in user; none of it is assumed.  A connection that was not confirmed
 * is never described as connected, and a key that was never tested is never
 * described as working.
 */

function when(iso: string | null | undefined): string {
  if (!iso) return 'at an unknown time';
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return 'at an unknown time';
  return `on ${parsed.toISOString().slice(0, 10)}`;
}

/** What is known about the Hevy connection right now. */
export function hevyFact(account: SetupAccount): string {
  const operation = account.connector?.operation;
  if (operation === 'connecting') return 'Checking your API key with Hevy…';
  if (operation === 'syncing') return 'Syncing your workout history…';
  if (!account.checks['hevy_connected']) {
    return account.reason_code === 'credential_removed'
      ? 'The stored API key was removed.'
      : 'Not connected.';
  }
  switch (account.connection_state) {
    case 'connected':
      return 'Connected — Hevy accepted your API key.';
    case 'invalid':
      return 'Hevy rejected the stored API key.';
    case 'insufficient_scope':
      return 'The stored API key cannot read your workouts.';
    case 'unverified':
      return 'The last check could not reach Hevy, so the connection is unconfirmed.';
    default:
      return 'A key is stored but has not been verified yet.';
  }
}

/** How the most recent history sync ended. */
export function lastSyncFact(account: SetupAccount): string {
  const connector = account.connector;
  if (connector?.operation === 'syncing') return 'A sync is running now.';
  if (connector?.operation_stalled) {
    return 'A previous sync never finished. Run it again.';
  }
  const last = connector?.last_sync;
  if (!last) return 'No sync has been recorded yet.';
  if (last.outcome === 'failed') {
    return `Failed ${when(last.at)}. Your existing history was not changed.`;
  }
  const count =
    typeof last.records === 'number'
      ? ` — ${last.records} ${last.records === 1 ? 'workout' : 'workouts'} found`
      : '';
  return `Completed ${when(last.at)}${count}.`;
}

/** What is known about the AI provider behind Coach. */
export function aiProviderFact(account: SetupAccount): string {
  switch (account.ai_provider_state) {
    case 'verified':
      return 'Verified — your key passed a connection test.';
    case 'unverified':
      return account.ai_provider_source === 'server'
        ? 'Using the shared server key, which cannot be tested for your account.'
        : 'A key is configured but has not been tested. Use Test in the AI Provider section.';
    case 'failed':
      return 'The last connection test failed. Test it again or replace the key.';
    case 'unsupported':
      return 'The selected provider is not supported.';
    case 'missing_key':
      return 'No key configured.';
    default:
      return 'Not checked.';
  }
}

export interface KeyVerification {
  label: string;
  detail: string;
  /** True only when a connection test passed. */
  verified: boolean;
}

/** How a stored AI key should be labelled from its last connection test. */
export function keyVerification(
  hasKey: boolean | undefined,
  lastTestStatus: string | null | undefined,
  lastTestedAt?: string | null,
): KeyVerification {
  if (!hasKey) {
    return { label: 'No key', detail: 'Add a key to use Coach.', verified: false };
  }
  const status = (lastTestStatus ?? '').trim().toLowerCase();
  if (!status) {
    return {
      label: 'Not tested',
      detail: 'Saved, but never tested. Use Test to confirm it works.',
      verified: false,
    };
  }
  if (status === 'ok') {
    return {
      label: 'Verified',
      detail: `Passed a connection test ${when(lastTestedAt)}.`,
      verified: true,
    };
  }
  return {
    label: 'Test failed',
    detail: `The last connection test failed ${when(lastTestedAt)}. Test again or replace the key.`,
    verified: false,
  };
}
