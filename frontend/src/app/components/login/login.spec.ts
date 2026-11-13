import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { Login } from './login';

async function render(query: Record<string, string>): Promise<HTMLElement> {
  await TestBed.configureTestingModule({
    imports: [Login],
    providers: [
      provideHttpClient(),
      provideHttpClientTesting(),
      {
        provide: ActivatedRoute,
        useValue: { snapshot: { queryParamMap: convertToParamMap(query) } },
      },
    ],
  }).compileComponents();
  const fixture: ComponentFixture<Login> = TestBed.createComponent(Login);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

describe('Login access-denied messaging', () => {
  it('shows no alert on a plain visit', async () => {
    const page = await render({});
    expect(page.querySelector('[role="alert"]')).toBeNull();
    expect(page.querySelector('a[href="/login/google"]')).not.toBeNull();
  });

  it('shows one generic message for a refused sign-in', async () => {
    const page = await render({ error: 'access_denied' });
    const alert = page.querySelector('[role="alert"]');
    expect(alert?.textContent).toContain("can't be used to sign in here");
    // Never says which rule applied: nothing about allowlists, invitations,
    // suspension or deletion.
    expect(alert?.textContent?.toLowerCase()).not.toMatch(/allow|invit|suspend|delet/);
  });

  it('shows a distinct, non-accusatory message when sign-in is unavailable', async () => {
    const page = await render({ error: 'unavailable' });
    expect(page.querySelector('[role="alert"]')?.textContent).toContain('temporarily unavailable');
  });

  it('carries the intended destination through the OAuth start link', async () => {
    const page = await render({ next: '/plan?week=2' });
    expect(page.querySelector('a')?.getAttribute('href')).toBe(
      '/login/google?next=%2Fplan%3Fweek%3D2',
    );
  });

  it('never echoes an unknown or hostile error value', async () => {
    for (const hostile of [
      '<img src=x onerror=alert(1)>',
      'constructor',
      '__proto__',
      'toString',
    ]) {
      TestBed.resetTestingModule();
      const page = await render({ error: hostile });
      expect(page.querySelector('[role="alert"]')).toBeNull();
      expect(page.innerHTML).not.toContain('onerror');
    }
  });
});
