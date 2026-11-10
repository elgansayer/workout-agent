import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { Login } from './login';

interface Rendered {
  page: HTMLElement;
  fixture: ComponentFixture<Login>;
  http: HttpTestingController;
}

async function renderPage(query: Record<string, string>): Promise<Rendered> {
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
  return {
    page: fixture.nativeElement as HTMLElement,
    fixture,
    http: TestBed.inject(HttpTestingController),
  };
}

async function render(query: Record<string, string>): Promise<HTMLElement> {
  return (await renderPage(query)).page;
}

/** Answer the page's request for what it may say about this deployment. */
async function answerConfig(
  rendered: Rendered,
  body: string | object | null,
  failure?: { status: number; statusText: string },
): Promise<void> {
  const request = rendered.http.expectOne('/api/auth/config');
  expect(request.request.method).toBe('GET');
  if (failure) request.flush(null, failure);
  else request.flush(body);
  // The page reads the answer through a few chained promises; a macrotask
  // turn lets all of them settle before the view is checked.
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
  rendered.fixture.detectChanges();
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

describe('Login registration wording', () => {
  const note = (page: HTMLElement) =>
    page.querySelector('[data-testid="registration-note"]')?.textContent?.trim();

  it('says nothing about registration until the server has answered', async () => {
    const rendered = await renderPage({});
    expect(note(rendered.page)).toBeUndefined();
    await answerConfig(rendered, { google: true, registration: 'open' });
  });

  it('tells a newcomer on an open deployment that signing in creates their account', async () => {
    const rendered = await renderPage({});
    await answerConfig(rendered, { google: true, registration: 'open' });
    expect(note(rendered.page)).toContain('creates your account');
  });

  it('tells a visitor to an invite-only deployment to use the invited Google account', async () => {
    const rendered = await renderPage({});
    await answerConfig(rendered, { google: true, registration: 'invite_only' });
    expect(note(rendered.page)).toContain('by invitation');
    expect(note(rendered.page)).toContain('invitation was sent to');
  });

  it('says access is limited on a restricted deployment', async () => {
    const rendered = await renderPage({});
    await answerConfig(rendered, { google: true, registration: 'restricted' });
    expect(note(rendered.page)).toContain('limited to approved Google accounts');
  });

  it('keeps the sign-in button and falls back to generic wording when the mode cannot be read', async () => {
    const rendered = await renderPage({});
    await answerConfig(rendered, null, { status: 500, statusText: 'Server Error' });
    expect(note(rendered.page)).toBeUndefined();
    expect(rendered.page.querySelector('a[href="/login/google"]')).not.toBeNull();
  });

  it('ignores an answer that is not exactly what the server sends', async () => {
    for (const body of [
      { google: true, registration: 'anything-goes' },
      { google: 'yes', registration: 'open' },
      { registration: 'open' },
      'open',
      null,
    ]) {
      TestBed.resetTestingModule();
      const rendered = await renderPage({});
      await answerConfig(rendered, body);
      expect(note(rendered.page)).toBeUndefined();
      expect(rendered.page.querySelector('a[href="/login/google"]')).not.toBeNull();
    }
  });

  it('explains instead of showing a dead button when Google sign-in is not configured', async () => {
    const rendered = await renderPage({});
    await answerConfig(rendered, { google: false, registration: 'open' });
    expect(rendered.page.querySelector('a.google-btn')).toBeNull();
    expect(
      rendered.page.querySelector('[data-testid="google-not-configured"]')?.textContent,
    ).toContain('not set up');
  });
});

describe('Login destination', () => {
  const button = (page: HTMLElement) => page.querySelector('a.google-btn')?.getAttribute('href');

  it('resumes at the page the visitor was headed to once they have signed in', async () => {
    const page = await render({ next: '/plan?week=2&x=1' });
    expect(button(page)).toBe('/login/google?next=%2Fplan%3Fweek%3D2%26x%3D1');
  });

  it('keeps the destination alongside an access-denied message so a retry still resumes', async () => {
    const page = await render({ error: 'access_denied', next: '/history' });
    expect(page.querySelector('[role="alert"]')).not.toBeNull();
    expect(button(page)).toBe('/login/google?next=%2Fhistory');
  });

  it('uses the plain link when there is nothing to resume', async () => {
    expect(button(await render({}))).toBe('/login/google');
    TestBed.resetTestingModule();
    expect(button(await render({ next: '/' }))).toBe('/login/google');
  });

  it('never builds a link to an unsafe destination', async () => {
    for (const hostile of [
      '//evil.example',
      'https://evil.example/plan',
      'javascript:alert(1)',
      '/\\evil.example',
      '/%2F/evil.example',
      '/login',
      '/api/me',
      '/plan\r\nSet-Cookie: x=1',
    ]) {
      TestBed.resetTestingModule();
      const page = await render({ next: hostile });
      expect(button(page), hostile).toBe('/login/google');
      expect(page.innerHTML).not.toContain('evil');
    }
  });
});
