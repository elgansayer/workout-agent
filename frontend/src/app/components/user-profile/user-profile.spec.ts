import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { UserProfile } from './user-profile';

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

describe('UserProfile active sessions', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [UserProfile],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function loadProfile() {
    const fixture = TestBed.createComponent(UserProfile);
    fixture.componentInstance.authService.currentUser.set({
      id: 'user-1',
      email: 'first@example.test',
    });
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
