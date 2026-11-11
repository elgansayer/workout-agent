import { SetupAccount } from '../../services/setup-state.service';
import { aiProviderFact, hevyFact, keyVerification, lastSyncFact } from './settings.readiness';

function account(overrides: Partial<SetupAccount> = {}): SetupAccount {
  return {
    state: 'ready',
    label: 'Your training data is up to date',
    reason: '',
    reason_code: null,
    first_run: false,
    checks: { hevy_connected: true },
    missing: [],
    last_activity_at: null,
    age_days: null,
    stale: false,
    connection_state: 'connected',
    connector: null,
    ai_provider_state: 'verified',
    ...overrides,
  };
}

function connector(overrides: Partial<NonNullable<SetupAccount['connector']>> = {}) {
  return {
    provider: 'hevy',
    operation: null,
    operation_started_at: null,
    operation_stalled: false,
    last_sync: null,
    ...overrides,
  };
}

describe('settings readiness facts', () => {
  describe('hevyFact', () => {
    it('only says connected when Hevy accepted the key', () => {
      expect(hevyFact(account())).toContain('Connected');
      for (const state of ['invalid', 'insufficient_scope', 'unverified']) {
        expect(hevyFact(account({ connection_state: state }))).not.toMatch(/^Connected/);
      }
      expect(hevyFact(account({ connection_state: null }))).toContain('not been verified');
    });

    it('explains a rejected key, a missing scope and an unreachable Hevy distinctly', () => {
      expect(hevyFact(account({ connection_state: 'invalid' }))).toContain('rejected');
      expect(hevyFact(account({ connection_state: 'insufficient_scope' }))).toContain(
        'cannot read your workouts',
      );
      expect(hevyFact(account({ connection_state: 'unverified' }))).toContain('unconfirmed');
    });

    it('distinguishes a removed key from one never added', () => {
      const none = { hevy_connected: false };
      expect(hevyFact(account({ checks: none, connection_state: null }))).toBe('Not connected.');
      expect(
        hevyFact(
          account({ checks: none, connection_state: null, reason_code: 'credential_removed' }),
        ),
      ).toContain('removed');
    });

    it('reports a running check or sync before anything else', () => {
      expect(hevyFact(account({ connector: connector({ operation: 'connecting' }) }))).toContain(
        'Checking',
      );
      expect(hevyFact(account({ connector: connector({ operation: 'syncing' }) }))).toContain(
        'Syncing',
      );
    });
  });

  describe('lastSyncFact', () => {
    it('does not invent a sync that never happened', () => {
      expect(lastSyncFact(account())).toBe('No sync has been recorded yet.');
      expect(lastSyncFact(account({ connector: connector() }))).toBe(
        'No sync has been recorded yet.',
      );
    });

    it('reports a failure with its date and that history was kept', () => {
      const text = lastSyncFact(
        account({
          connector: connector({
            last_sync: {
              outcome: 'failed',
              at: '2026-09-01T10:00:00+00:00',
              code: 'hevy_unavailable',
              records: null,
            },
          }),
        }),
      );

      expect(text).toContain('Failed on 2026-09-01');
      expect(text).toContain('existing history was not changed');
    });

    it('reports a success with how many workouts were found', () => {
      const completed = (records: number | null) =>
        lastSyncFact(
          account({
            connector: connector({
              last_sync: {
                outcome: 'succeeded',
                at: '2026-09-02T10:00:00+00:00',
                code: null,
                records,
              },
            }),
          }),
        );

      expect(completed(40)).toBe('Completed on 2026-09-02 — 40 workouts found.');
      expect(completed(1)).toContain('1 workout found');
      expect(completed(0)).toContain('0 workouts found');
      expect(completed(null)).toBe('Completed on 2026-09-02.');
    });

    it('reports a running sync and an abandoned one', () => {
      expect(lastSyncFact(account({ connector: connector({ operation: 'syncing' }) }))).toContain(
        'running now',
      );
      expect(
        lastSyncFact(account({ connector: connector({ operation_stalled: true }) })),
      ).toContain('never finished');
    });

    it('never throws on an unparsable timestamp', () => {
      expect(
        lastSyncFact(
          account({
            connector: connector({
              last_sync: { outcome: 'failed', at: 'garbage', code: null, records: null },
            }),
          }),
        ),
      ).toContain('at an unknown time');
    });
  });

  describe('aiProviderFact', () => {
    it('only says verified for a key that passed a connection test', () => {
      const verified = aiProviderFact(account({ ai_provider_state: 'verified' }));
      expect(verified).toContain('Verified');

      for (const state of ['unverified', 'failed', 'missing_key', 'unsupported', null]) {
        expect(aiProviderFact(account({ ai_provider_state: state }))).not.toContain('Verified');
      }
    });

    it('does not tell an account on the shared server key to test a key it does not own', () => {
      const shared = aiProviderFact(
        account({ ai_provider_state: 'unverified', ai_provider_source: 'server' }),
      );

      expect(shared).toContain('shared server key');
      expect(shared).not.toContain('Verified');
      expect(shared).not.toContain('Use Test');
    });

    it('tells the user what to do about an untested or failing key', () => {
      expect(aiProviderFact(account({ ai_provider_state: 'unverified' }))).toContain('Test');
      expect(aiProviderFact(account({ ai_provider_state: 'failed' }))).toContain('failed');
      expect(aiProviderFact(account({ ai_provider_state: 'missing_key' }))).toBe(
        'No key configured.',
      );
    });
  });

  describe('keyVerification', () => {
    it('labels a key by its last connection test, never by its presence', () => {
      expect(keyVerification(false, null).label).toBe('No key');
      expect(keyVerification(true, null)).toMatchObject({ label: 'Not tested', verified: false });
      expect(keyVerification(true, '')).toMatchObject({ label: 'Not tested', verified: false });
      expect(keyVerification(true, 'ok', '2026-09-03T00:00:00+00:00')).toMatchObject({
        label: 'Verified',
        verified: true,
      });
      expect(keyVerification(true, 'rejected')).toMatchObject({
        label: 'Test failed',
        verified: false,
      });
    });

    it('includes the test date and the way to recover', () => {
      expect(keyVerification(true, 'ok', '2026-09-03T00:00:00+00:00').detail).toContain(
        'on 2026-09-03',
      );
      expect(keyVerification(true, 'unreachable').detail).toContain('Test again or replace');
      expect(keyVerification(true, null).detail).toContain('Use Test');
    });
  });
});
