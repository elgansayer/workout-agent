import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { IntegrationState, IntegrationStatus, STATE_LABELS } from './integration-status';
import { StateBadge, StatusNotice } from './status-widgets';

const status = (state: IntegrationState, overrides: Partial<IntegrationStatus> = {}) =>
  ({
    connector: 'hevy',
    label: 'Hevy',
    state,
    reason: 'synthetic',
    message: `Hevy is ${state}.`,
    next_action: 'sync',
    action_label: 'Sync now',
    action_href: '/settings?section=connections',
    retryable: false,
    freshness: null,
    last_attempt_at: null,
    last_success_at: null,
    last_error_code: null,
    consecutive_failures: 0,
    last_run: null,
    ...overrides,
  }) as IntegrationStatus;

@Component({
  standalone: true,
  imports: [StateBadge, StatusNotice],
  template: `
    <app-state-badge [state]="state()" />
    <app-status-notice
      [status]="current()"
      [showReady]="showReady()"
      [inline]="inline()"
      [showAction]="showAction()"
    />
  `,
})
class Host {
  state = signal<string | null | undefined>('ready');
  current = signal<IntegrationStatus | null | undefined>(null);
  showReady = signal(false);
  inline = signal(false);
  showAction = signal(true);
}

function render() {
  TestBed.configureTestingModule({ imports: [Host] });
  const fixture = TestBed.createComponent(Host);
  fixture.detectChanges();
  const el = fixture.nativeElement as HTMLElement;
  const update = (apply: (host: Host) => void) => {
    apply(fixture.componentInstance);
    fixture.detectChanges();
  };
  return { el, update };
}

describe('shared integration widgets', () => {
  it.each(Object.entries(STATE_LABELS))('labels the %s state with text', (state, label) => {
    const { el, update } = render();
    update((host) => host.state.set(state));
    const badge = el.querySelector('.state-badge');
    expect(badge?.textContent?.trim()).toBe(label);
    expect(badge?.getAttribute('data-state')).toBe(state);
  });

  it('renders no badge for a missing or unknown state', () => {
    const { el, update } = render();
    update((host) => host.state.set(undefined));
    expect(el.querySelector('.state-badge')).toBeNull();
    update((host) => host.state.set('exploded'));
    expect(el.querySelector('.state-badge')).toBeNull();
  });

  it('stays quiet for ready connectors unless asked to show them', () => {
    const { el, update } = render();
    update((host) => host.current.set(status('ready')));
    expect(el.querySelector('.status-notice')).toBeNull();
    update((host) => host.showReady.set(true));
    expect(el.querySelector('.status-notice')?.textContent).toContain('Hevy is ready.');
  });

  it('renders nothing without a status', () => {
    const { el, update } = render();
    update((host) => host.current.set(undefined));
    expect(el.querySelector('.status-notice')).toBeNull();
  });

  it.each([
    ['degraded', 'alert'],
    ['setup_required', 'alert'],
    ['stale', 'status'],
    ['connecting', 'status'],
    ['syncing', 'status'],
  ] as const)('announces %s with role %s', (state, role) => {
    const { el, update } = render();
    update((host) => host.current.set(status(state)));
    const notice = el.querySelector('.status-notice');
    expect(notice?.getAttribute('role')).toBe(role);
    expect(notice?.getAttribute('data-state')).toBe(state);
    expect(notice?.classList.contains('panel')).toBe(true);
  });

  it('shows the label, state and a keyboard-reachable action', () => {
    const { el, update } = render();
    update((host) => host.current.set(status('stale')));
    expect(el.querySelector('.status-notice-head')?.textContent).toContain('Hevy');
    expect(el.querySelector('.status-notice .state-badge')?.textContent?.trim()).toBe('Stale');
    const link = el.querySelector<HTMLAnchorElement>('.status-notice a');
    expect(link?.textContent?.trim()).toBe('Sync now');
    expect(link?.getAttribute('href')).toBe('/settings?section=connections');
  });

  it('omits the action when the connector offers none or the page hosts it', () => {
    const { el, update } = render();
    update((host) =>
      host.current.set(status('connecting', { action_href: null, action_label: null })),
    );
    expect(el.querySelector('.status-notice a')).toBeNull();
    update((host) => {
      host.current.set(status('stale'));
      host.showAction.set(false);
    });
    expect(el.querySelector('.status-notice a')).toBeNull();
  });

  it('renders inline without panel chrome or a duplicate heading', () => {
    const { el, update } = render();
    update((host) => {
      host.current.set(status('degraded'));
      host.inline.set(true);
    });
    const notice = el.querySelector('.status-notice');
    expect(notice?.classList.contains('status-inline')).toBe(true);
    expect(notice?.classList.contains('panel')).toBe(false);
    expect(el.querySelector('.status-notice-head')).toBeNull();
  });

  it('treats server messages as text, never markup', () => {
    const { el, update } = render();
    update((host) =>
      host.current.set(status('degraded', { message: '<img src=x onerror=alert(1)>' })),
    );
    expect(el.querySelector('.status-notice img')).toBeNull();
    expect(el.querySelector('.status-notice-message')?.textContent).toBe(
      '<img src=x onerror=alert(1)>',
    );
  });
});
