import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import type { MockInstance } from 'vitest';

import { Auth } from '../../services/auth';
import { UserProfile } from './user-profile';

describe('UserProfile sessions', () => {
  const user = { id: 'user-1', email: 'athlete@example.test', name: 'Synthetic Athlete' };
  let auth: {
    currentUser: ReturnType<typeof signal<typeof user | null>>;
    logout: ReturnType<typeof vi.fn>;
    logoutAllDevices: ReturnType<typeof vi.fn>;
  };
  let fixture: ComponentFixture<UserProfile>;
  let confirmSpy: MockInstance<typeof window.confirm>;

  beforeEach(async () => {
    auth = {
      currentUser: signal<typeof user | null>(user),
      logout: vi.fn().mockResolvedValue(undefined),
      logoutAllDevices: vi.fn().mockResolvedValue(undefined),
    };
    confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    await TestBed.configureTestingModule({
      imports: [UserProfile],
      providers: [{ provide: Auth, useValue: auth }],
    }).compileComponents();
    fixture = TestBed.createComponent(UserProfile);
    fixture.detectChanges();
  });

  afterEach(() => confirmSpy.mockRestore());

  function button(label: string): HTMLButtonElement {
    const found = Array.from(fixture.nativeElement.querySelectorAll('button')).find(
      (element) => (element as HTMLElement).textContent?.trim() === label,
    );
    expect(found, `button "${label}"`).toBeTruthy();
    return found as HTMLButtonElement;
  }

  it('offers an accessible, explained control to sign out of every device', () => {
    const control = button('Sign out of all devices');

    expect(control.type).toBe('button');
    const help = fixture.nativeElement.querySelector(
      `#${control.getAttribute('aria-describedby')}`,
    ) as HTMLElement;
    expect(help.textContent).toContain('every browser and device');
    expect(
      fixture.nativeElement.querySelector('section[aria-labelledby="sessions-heading"] h2')
        ?.textContent,
    ).toContain('Sessions');
  });

  it('signs out this browser from the existing Log Out button', () => {
    button('Log Out').click();

    expect(auth.logout).toHaveBeenCalledTimes(1);
    expect(auth.logoutAllDevices).not.toHaveBeenCalled();
  });

  it('does nothing unless the user confirms', async () => {
    confirmSpy.mockReturnValue(false);

    button('Sign out of all devices').click();
    await fixture.whenStable();

    expect(auth.logoutAllDevices).not.toHaveBeenCalled();
  });

  it('revokes every session after confirmation and stays busy while navigating away', async () => {
    button('Sign out of all devices').click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(auth.logoutAllDevices).toHaveBeenCalledTimes(1);
    expect(button('Sign out of all devices').disabled).toBe(true);
    expect(fixture.nativeElement.querySelector('[role="alert"]')).toBeNull();
  });

  it('announces a failure and lets the user retry', async () => {
    auth.logoutAllDevices.mockRejectedValueOnce(new Error('server unavailable'));

    button('Sign out of all devices').click();
    await fixture.whenStable();
    fixture.detectChanges();

    const alert = fixture.nativeElement.querySelector('[role="alert"]') as HTMLElement;
    expect(alert.textContent).toContain('could not sign you out of all devices');
    expect(button('Sign out of all devices').disabled).toBe(false);

    button('Sign out of all devices').click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(auth.logoutAllDevices).toHaveBeenCalledTimes(2);
    expect(fixture.nativeElement.querySelector('[role="alert"]')).toBeNull();
  });
});
