import { signal } from '@angular/core';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import type { MockInstance } from 'vitest';

import { Auth } from '../../services/auth';
import { UserProfile } from './user-profile';

const user = { id: 'user-1', email: 'athlete@example.test', name: 'Synthetic Athlete' };

const emptySessions = { sessions: [] };

const sessionResponse = {
  sessions: [
    {
      id: 7,
      device: 'Chrome on Linux',
      location: 'Country GB · Approx. network 203.0.113.0/24',
      created_at: '2026-11-10T09:00:00+00:00',
      last_used_at: '2026-11-10T10:00:00+00:00',
      current: true,
    },
    {
      id: 8,
      device: 'Safari on iOS',
      location: 'Local network',
      created_at: '2026-11-09T09:00:00+00:00',
      last_used_at: '2026-11-09T10:00:00+00:00',
      current: false,
    },
  ],
};

function authMock() {
  return {
    currentUser: signal<typeof user | null>(user),
    logout: vi.fn().mockResolvedValue(undefined),
    logoutAllDevices: vi.fn().mockResolvedValue(undefined),
  };
}

describe('UserProfile sessions', () => {
  let auth: ReturnType<typeof authMock>;
  let fixture: ComponentFixture<UserProfile>;
  let http: HttpTestingController;
  let confirmSpy: MockInstance<typeof window.confirm>;

  beforeEach(async () => {
    auth = authMock();
    confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    await TestBed.configureTestingModule({
      imports: [UserProfile],
      providers: [
        { provide: Auth, useValue: auth },
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(UserProfile);
    fixture.detectChanges();
    http.expectOne('/api/sessions').flush(emptySessions);
  });

  afterEach(() => {
    http.verify();
    confirmSpy.mockRestore();
  });

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

describe('UserProfile active sessions', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [UserProfile],
      providers: [
        { provide: Auth, useValue: authMock() },
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function loadProfile() {
    const fixture = TestBed.createComponent(UserProfile);
    fixture.detectChanges();
    http.expectOne('/api/sessions').flush(sessionResponse);
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  it('renders safe device, location, creation and last-used details', async () => {
    const fixture = await loadProfile();
    const element = fixture.nativeElement as HTMLElement;
    const text = element.textContent || '';

    expect(text).toContain('Chrome on Linux');
    expect(text).toContain('Safari on iOS');
    expect(text).toContain('Approx. network 203.0.113.0/24');
    expect(text).toContain('This device');
    expect(text).toContain('Created');
    expect(text).toContain('Last used');
  });

  it('revokes a selected session through the API', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const fixture = await loadProfile();
    const component = fixture.componentInstance;

    component.revokeSession(component.sessions()[1]);

    const revoke = http.expectOne('/api/sessions/8');
    expect(revoke.request.method).toBe('DELETE');
    revoke.flush({ status: 'ok', logged_out: false });
    expect(component.sessionStatus()).toContain('Session revoked');

    http.expectOne('/api/sessions').flush(sessionResponse);
  });

  it('revokes all other sessions and refreshes the list', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const fixture = await loadProfile();
    const component = fixture.componentInstance;

    component.revokeOtherSessions();

    const revoke = http.expectOne('/api/sessions/revoke-others');
    expect(revoke.request.method).toBe('POST');
    revoke.flush({ status: 'ok', revoked: 1 });
    expect(component.sessionStatus()).toContain('Revoked 1 other session');

    http.expectOne('/api/sessions').flush(sessionResponse);
  });
});

describe('UserProfile e-mail change', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [UserProfile],
      providers: [
        { provide: Auth, useValue: authMock() },
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function loadProfile() {
    const fixture = TestBed.createComponent(UserProfile);
    fixture.detectChanges();
    http.expectOne('/api/sessions').flush(emptySessions);
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  it('starts a verified change for the entered address', async () => {
    const fixture = await loadProfile();
    const element = fixture.nativeElement as HTMLElement;
    const input = element.querySelector('#new-email') as HTMLInputElement;
    input.value = 'new-address@example.test';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();

    const form = element.querySelector('form') as HTMLFormElement;
    form.dispatchEvent(new Event('submit'));
    const request = http.expectOne('/api/account/email/change-request');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({ new_email: 'new-address@example.test' });
    request.flush({ status: 'pending', message: 'Check the new address.' });
    fixture.detectChanges();

    expect(element.textContent).toContain('Check the new address.');
  });

  it('shows the server reason when recent authentication is required', async () => {
    const fixture = await loadProfile();
    const element = fixture.nativeElement as HTMLElement;
    const input = element.querySelector('#new-email') as HTMLInputElement;
    input.value = 'new-address@example.test';
    input.dispatchEvent(new Event('input'));

    (element.querySelector('form') as HTMLFormElement).dispatchEvent(new Event('submit'));
    const request = http.expectOne('/api/account/email/change-request');
    request.flush(
      { detail: 'Sign in again before changing the account e-mail address.' },
      { status: 403, statusText: 'Forbidden' },
    );
    fixture.detectChanges();

    expect(element.querySelector('[role="alert"]')?.textContent).toContain('Sign in again');
  });
});
