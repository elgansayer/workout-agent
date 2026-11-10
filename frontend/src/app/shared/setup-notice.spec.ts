import { ComponentFixture, TestBed } from '@angular/core/testing';
import { SetupNotice } from './setup-notice';
import { SetupState } from './setup-state';

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

  it('renders default setup notice for new state', () => {
    const setup: SetupState = {
      state: 'new',
      label: 'Finish setting up your training data',
      reason: 'Hevy is not connected',
      action: {
        id: 'connect_hevy',
        label: 'Connect Hevy',
        href: '/settings',
        description: 'Add your Hevy API key in Settings.',
      },
      missing: ['hevy_connected'],
      last_activity_at: null,
      age_days: null,
      stale: false,
    };

    fixture.componentRef.setInput('setup', setup);
    fixture.detectChanges();

    const compiled = fixture.nativeElement as HTMLElement;
    expect(compiled.querySelector('.eyebrow')?.textContent).toContain('Setup required');
    expect(compiled.querySelector('h2')?.textContent).toContain('Finish setting up your training data');
    expect(compiled.textContent).toContain('Hevy is not connected');

    const link = compiled.querySelector('a.btn') as HTMLAnchorElement | null;
    expect(link).toBeTruthy();
    expect(link?.getAttribute('href')).toBe('/settings');
    expect(link?.textContent).toContain('Connect Hevy');
  });

  it('respects heading and reason overrides', () => {
    const setup: SetupState = {
      state: 'disconnected',
      label: 'Hevy is no longer connected',
      reason: 'Key removed',
      action: null,
      missing: ['hevy_connected'],
      last_activity_at: null,
      age_days: null,
      stale: false,
    };

    fixture.componentRef.setInput('setup', setup);
    fixture.componentRef.setInput('headingOverride', 'Custom heading');
    fixture.componentRef.setInput('reasonOverride', 'Custom explanation');
    fixture.detectChanges();

    const compiled = fixture.nativeElement as HTMLElement;
    expect(compiled.querySelector('.eyebrow')?.textContent).toContain('Connection removed');
    expect(compiled.querySelector('h2')?.textContent).toContain('Custom heading');
    expect(compiled.textContent).toContain('Custom explanation');
  });
});
