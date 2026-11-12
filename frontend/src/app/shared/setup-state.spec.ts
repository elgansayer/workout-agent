import {
  SetupState,
  SetupStateKind,
  isLive,
  navBadge,
  needsAttention,
  needsSetup,
  resolveViewState,
  setupAction,
} from './setup-state';

function setup(state: SetupStateKind, overrides: Partial<SetupState> = {}): SetupState {
  return {
    state,
    label: state,
    reason: '',
    action: null,
    missing: [],
    last_activity_at: null,
    age_days: null,
    stale: false,
    ...overrides,
  };
}

const ALL_STATES: SetupStateKind[] = [
  'setup_required',
  'connecting',
  'syncing',
  'ready',
  'stale',
  'degraded',
];

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

    it('returns attention when there is data but a notice must accompany it', () => {
      expect(
        resolveViewState({
          loading: false,
          error: null,
          hasData: true,
          attention: true,
        }),
      ).toBe('attention');
    });

    it('returns ready when has data and nothing needs attention', () => {
      expect(
        resolveViewState({
          loading: false,
          error: null,
          hasData: true,
          attention: false,
        }),
      ).toBe('ready');
    });

    it('keeps a failed request distinguishable from an empty account', () => {
      const failed = resolveViewState({ loading: false, error: 'x', hasData: false });
      const empty = resolveViewState({ loading: false, error: null, hasData: false });
      expect(failed).not.toBe(empty);
    });
  });

  describe('setupAction', () => {
    it('returns null for missing setup or ready state', () => {
      expect(setupAction(null)).toBeNull();
      expect(setupAction(undefined)).toBeNull();
      expect(
        setupAction(
          setup('ready', {
            action: { id: 'sync', label: 'Sync', href: '/settings', description: '' },
          }),
        ),
      ).toBeNull();
    });

    it('returns the action when state is not ready', () => {
      const action = {
        id: 'connect_hevy',
        label: 'Connect Hevy',
        href: '/settings',
        description: 'Connect account',
      };
      expect(setupAction(setup('setup_required', { action }))).toEqual(action);
    });

    it('offers nothing while a connect or sync is running', () => {
      expect(setupAction(setup('syncing'))).toBeNull();
      expect(setupAction(setup('connecting'))).toBeNull();
    });
  });

  describe('readiness helpers', () => {
    it('only a ready state needs no setup or attention', () => {
      for (const state of ALL_STATES) {
        const expected = state !== 'ready';
        expect(needsSetup(setup(state))).toBe(expected);
        expect(needsAttention(setup(state))).toBe(expected);
      }
      expect(needsSetup(null)).toBe(false);
      expect(needsAttention(undefined)).toBe(false);
    });

    it('treats only connecting and syncing as live', () => {
      expect(ALL_STATES.filter((state) => isLive(setup(state)))).toEqual(['connecting', 'syncing']);
      expect(isLive(null)).toBe(false);
    });

    it('gives every non-ready state its own badge and none to ready', () => {
      const badges = ALL_STATES.map((state) => navBadge(setup(state)));

      expect(badges[ALL_STATES.indexOf('ready')]).toBeNull();
      const labels = badges.filter((badge) => badge !== null).map((badge) => badge!.label);
      expect(labels).toEqual(['Setup', 'Connecting', 'Syncing', 'Stale', 'Attention']);
      expect(navBadge(setup('degraded'))?.spoken).toBe('needs attention');
      expect(navBadge(setup('syncing'))?.spoken).toBe('syncing');
      expect(navBadge(null)).toBeNull();
    });
  });
});
