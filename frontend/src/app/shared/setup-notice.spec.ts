import { ComponentFixture, TestBed } from '@angular/core/testing';
import { SetupNotice } from './setup-notice';
import { SetupState, SetupStateKind } from './setup-state';

function setup(state: SetupStateKind, overrides: Partial<SetupState> = {}): SetupState {
  return {
    state,
    label: 'Label',
    reason: '',
    action: null,
    missing: [],
    last_activity_at: null,
    age_days: null,
    stale: false,
    ...overrides,
  };
}

describe('SetupNotice', () => {
  let fixture: ComponentFixture<SetupNotice>;
  let component: SetupNotice;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [SetupNotice],
    }).compileComponents();

    fixture = TestBed.createComponent(SetupNotice);
    component = fixture.componentInstance;
  });

  function render(state: SetupState): HTMLElement {
    fixture.componentRef.setInput('setup', state);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('welcomes a brand-new account and offers the first step', () => {
    const compiled = render(
      setup('setup_required', {
        label: 'Finish setting up your training data',
        reason: 'Hevy is not connected',
        first_run: true,
        action: {
          id: 'connect_hevy',
          label: 'Connect Hevy',
          href: '/settings',
          description: 'Add your Hevy API key in Settings.',
        },
        missing: ['hevy_connected'],
      }),
    );

    expect(compiled.querySelector('.eyebrow')?.textContent).toContain('Welcome');
    expect(compiled.querySelector('h2')?.textContent).toContain(
      'Finish setting up your training data',
    );
    expect(compiled.textContent).toContain('Hevy is not connected');

    const link = compiled.querySelector('a.btn') as HTMLAnchorElement | null;
    expect(link?.getAttribute('href')).toBe('/settings');
    expect(link?.textContent).toContain('Connect Hevy');
  });

  it('says setup is incomplete for an account that is part-way through', () => {
    const compiled = render(setup('setup_required', { first_run: false }));

    expect(compiled.querySelector('.eyebrow')?.textContent).toContain('Setup incomplete');
    expect(compiled.querySelector('section')?.getAttribute('role')).toBeNull();
  });

  it('announces a running sync politely, marks it busy and offers no step', () => {
    const compiled = render(
      setup('syncing', {
        label: 'Syncing your Hevy history',
        reason: 'A Hevy sync started 3 minutes ago is still running.',
      }),
    );
    const section = compiled.querySelector('section');

    expect(compiled.querySelector('.eyebrow')?.textContent).toContain('Sync in progress');
    expect(section?.getAttribute('role')).toBe('status');
    expect(section?.getAttribute('aria-busy')).toBe('true');
    expect(compiled.textContent).toContain('still running');
    expect(compiled.querySelector('a.btn')).toBeNull();
  });

  it('announces a connection check as connecting', () => {
    const compiled = render(setup('connecting', { reason: 'Checking your Hevy API key.' }));

    expect(compiled.querySelector('.eyebrow')?.textContent).toContain('Connecting');
    expect(compiled.querySelector('section')?.getAttribute('aria-busy')).toBe('true');
  });

  it('announces a failure as an alert with the way to repair it', () => {
    const compiled = render(
      setup('degraded', {
        label: 'Your Hevy connection needs attention',
        reason: 'Hevy rejected your stored API key.',
        reason_code: 'credential_rejected',
        action: {
          id: 'reconnect_hevy',
          label: 'Reconnect Hevy',
          href: '/settings',
          description: 'Verify or replace your Hevy API key.',
        },
      }),
    );
    const section = compiled.querySelector('section');

    expect(compiled.querySelector('.eyebrow')?.textContent).toContain('Needs attention');
    expect(section?.getAttribute('role')).toBe('alert');
    expect(section?.getAttribute('aria-busy')).toBeNull();
    expect(section?.getAttribute('data-state')).toBe('degraded');
    expect(compiled.querySelector('a.btn')?.textContent).toContain('Reconnect Hevy');
  });

  it('flags out-of-date data and shows when it was last recorded', () => {
    const compiled = render(
      setup('stale', {
        reason: 'Your last training activity was 30 days ago.',
        last_activity_at: '2026-08-01T00:00:00+00:00',
        action: { id: 'sync_hevy', label: 'Sync Hevy history', href: '/settings', description: '' },
      }),
    );

    expect(compiled.querySelector('.eyebrow')?.textContent).toContain('Data out of date');
    expect(compiled.querySelector('section')?.getAttribute('role')).toBe('status');
    expect(compiled.textContent).toContain('Last recorded activity');
  });

  it('respects heading and reason overrides', () => {
    fixture.componentRef.setInput('setup', setup('degraded', { reason: 'Key removed' }));
    fixture.componentRef.setInput('headingOverride', 'Custom heading');
    fixture.componentRef.setInput('reasonOverride', 'Custom explanation');
    fixture.detectChanges();

    const compiled = fixture.nativeElement as HTMLElement;
    expect(compiled.querySelector('.eyebrow')?.textContent).toContain('Needs attention');
    expect(compiled.querySelector('h2')?.textContent).toContain('Custom heading');
    expect(compiled.textContent).toContain('Custom explanation');
    expect(component.reason()).toBe('Custom explanation');
  });
});
