import {
  SetupState,
  needsSetup,
  resolveViewState,
  setupAction,
} from './setup-state';

describe('setup-state', () => {
  describe('resolveViewState', () => {
    it('returns failed when error is present', () => {
      expect(
        resolveViewState({
          loading: false,
          error: 'Network failure',
          hasData: false,
        }),
      ).toBe('failed');
    });

    it('returns loading when loading and no data', () => {
      expect(
        resolveViewState({
          loading: true,
          error: null,
          hasData: false,
        }),
      ).toBe('loading');
    });

    it('returns empty when not loading and no data', () => {
      expect(
        resolveViewState({
          loading: false,
          error: null,
          hasData: false,
        }),
      ).toBe('empty');
    });

    it('returns stale when has data and stale is true', () => {
      expect(
        resolveViewState({
          loading: false,
          error: null,
          hasData: true,
          stale: true,
        }),
      ).toBe('stale');
    });

    it('returns ready when has data and not stale', () => {
      expect(
        resolveViewState({
          loading: false,
          error: null,
          hasData: true,
          stale: false,
        }),
      ).toBe('ready');
    });
  });

  describe('setupAction', () => {
    it('returns null for missing setup or ready state', () => {
      expect(setupAction(null)).toBeNull();
      expect(setupAction(undefined)).toBeNull();
      expect(
        setupAction({
          state: 'ready',
          label: 'Ready',
          reason: '',
          action: {
            id: 'sync',
            label: 'Sync',
            href: '/settings',
            description: '',
          },
          missing: [],
          last_activity_at: null,
          age_days: null,
          stale: false,
        }),
      ).toBeNull();
    });

    it('returns the action when state is not ready', () => {
      const action = {
        id: 'connect_hevy',
        label: 'Connect Hevy',
        href: '/settings',
        description: 'Connect account',
      };
      const setup: SetupState = {
        state: 'new',
        label: 'Finish setting up',
        reason: 'Hevy not connected',
        action,
        missing: ['hevy_connected'],
        last_activity_at: null,
        age_days: null,
        stale: false,
      };
      expect(setupAction(setup)).toEqual(action);
    });
  });

  describe('needsSetup', () => {
    it('returns false for null, undefined or ready setup', () => {
      expect(needsSetup(null)).toBe(false);
      expect(needsSetup(undefined)).toBe(false);
      expect(
        needsSetup({
          state: 'ready',
          label: 'Ready',
          reason: '',
          action: null,
          missing: [],
          last_activity_at: null,
          age_days: null,
          stale: false,
        }),
      ).toBe(false);
    });

    it('returns true when state is not ready', () => {
      expect(
        needsSetup({
          state: 'new',
          label: 'Setup required',
          reason: '',
          action: null,
          missing: [],
          last_activity_at: null,
          age_days: null,
          stale: false,
        }),
      ).toBe(true);
    });
  });
});
