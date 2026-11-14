import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, Router } from '@angular/router';

import { Auth } from '../../services/auth';
import { Recovery } from './recovery';

async function render(query: Record<string, string>) {
  const router = { navigate: vi.fn().mockResolvedValue(true) };
  await TestBed.configureTestingModule({
    imports: [Recovery],
    providers: [
      provideHttpClient(),
      provideHttpClientTesting(),
      {
        provide: ActivatedRoute,
        useValue: { snapshot: { queryParamMap: convertToParamMap(query) } },
      },
      { provide: Router, useValue: router },
      { provide: Auth, useValue: { checkAuth: vi.fn().mockResolvedValue(undefined) } },
    ],
  }).compileComponents();
  const fixture = TestBed.createComponent(Recovery);
  fixture.detectChanges();
  const http = TestBed.inject(HttpTestingController);
  return { fixture, http, router, page: fixture.nativeElement as HTMLElement };
}

describe('Recovery page', () => {
  it('requests a link with the same visible answer on success and failure', async () => {
    const { fixture, http, page } = await render({});
    const input = page.querySelector('#recovery-email') as HTMLInputElement;
    input.value = 'person@example.test';
    input.dispatchEvent(new Event('input'));
    (page.querySelector('form') as HTMLFormElement).dispatchEvent(new Event('submit'));
    const request = http.expectOne('/api/auth/recovery/request');
    expect(request.request.body).toEqual({ email: 'person@example.test' });
    request.flush({ status: 'accepted' });
    fixture.detectChanges();
    expect(page.textContent).toContain('If that address belongs to an account');

    (page.querySelector('form') as HTMLFormElement).dispatchEvent(new Event('submit'));
    http
      .expectOne('/api/auth/recovery/request')
      .flush({ detail: 'Rate limit exceeded' }, { status: 429, statusText: 'Too Many Requests' });
    fixture.detectChanges();
    expect(page.textContent).toContain('If that address belongs to an account');
    http.verify();
  });

  it('confirms an e-mail change token through the change endpoint', async () => {
    const { fixture, http, page } = await render({
      token: 'synthetic-token',
      kind: 'email_change',
    });
    (page.querySelector('button') as HTMLButtonElement).click();
    const request = http.expectOne('/api/account/email/confirm');
    expect(request.request.body).toEqual({ token: 'synthetic-token' });
    request.flush({ status: 'ok' });
    fixture.detectChanges();
    expect(page.textContent).toContain('verified');
    http.verify();
  });

  it('confirms a recovery token and continues to the dashboard', async () => {
    const { fixture, http, router, page } = await render({ token: 'synthetic-token' });
    (page.querySelector('button') as HTMLButtonElement).click();
    http.expectOne('/api/auth/recovery/complete').flush({ status: 'ok' });
    await fixture.whenStable();
    expect(router.navigate).toHaveBeenCalledWith(['/dashboard']);
    http.verify();
  });
});
