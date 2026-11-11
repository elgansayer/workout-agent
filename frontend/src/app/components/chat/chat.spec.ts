import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { vi } from 'vitest';
import { environment } from '../../../environments/environment';
import { Chat } from './chat';

describe('Coach connection', () => {
  let chat: Chat;
  const originalApiUrl = environment.apiUrl;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    chat = TestBed.runInInjectionContext(() => new Chat());
  });

  afterEach(() => {
    environment.apiUrl = originalApiUrl;
    vi.unstubAllGlobals();
  });

  it('streams from the configured API origin with the signed-in session', async () => {
    environment.apiUrl = 'https://api.example.test/';
    const fetchMock = vi.fn().mockResolvedValue(
      new Response('Good session.', {
        headers: { 'Content-Type': 'text/plain; charset=utf-8' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    chat.userInput = 'How am I doing?';

    await chat.sendMessage();

    expect(fetchMock).toHaveBeenCalledWith(
      'https://api.example.test/api/rag_search?q=How%20am%20I%20doing%3F',
      expect.objectContaining({ credentials: 'include', cache: 'no-store' }),
    );
    expect(chat.messages().at(-1)?.content).toBe('Good session.');
    expect(chat.error()).toBeNull();
    expect(chat.isStreaming()).toBe(false);
  });

  it('shows configuration errors and keeps the question available to retry', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          new Response(
            JSON.stringify({
              detail: 'No AI provider key available. Add your own key in Settings.',
            }),
            { status: 400, headers: { 'Content-Type': 'application/json' } },
          ),
        ),
    );
    chat.userInput = 'Review my training';

    await chat.sendMessage();

    expect(chat.error()).toContain('Add your own key in Settings');
    expect(chat.userInput).toBe('Review my training');
    expect(chat.messages().some((message) => message.role === 'assistant')).toBe(false);
    expect(chat.isStreaming()).toBe(false);
  });

  it('does not show an HTML sign-in page as a successful coach reply', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response('<html>Sign in</html>', {
          headers: { 'Content-Type': 'text/html' },
        }),
      ),
    );
    chat.userInput = 'Hello';

    await chat.sendMessage();

    expect(chat.error()).toContain('Sign in again');
    expect(chat.messages()).toHaveLength(1);
  });
  it('does not submit an IME composition or a Shift+Enter newline', () => {
    const send = vi.spyOn(chat, 'sendMessage').mockResolvedValue();
    chat.onKeyDown(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true }));
    chat.onKeyDown(new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true }));
    expect(send).not.toHaveBeenCalled();
    chat.onKeyDown(new KeyboardEvent('keydown', { key: 'Enter' }));
    expect(send).toHaveBeenCalledOnce();
  });
});

describe('Coach shared readiness', () => {
  const account = (aiState: string, hevyState: string) => ({
    generated_at: '2026-11-11T12:00:00+00:00',
    state: aiState,
    components: {
      ai_provider: {
        connector: 'ai:claude',
        label: 'AI coach (Anthropic Claude)',
        state: aiState,
        message:
          aiState === 'ready'
            ? 'AI coach (Anthropic Claude) is configured.'
            : 'AI coach (Anthropic Claude) is not set up yet.',
        action_label: aiState === 'ready' ? null : 'Set up AI coach (Anthropic Claude)',
        action_href: aiState === 'ready' ? null : '/settings?section=ai',
      },
      hevy: {
        connector: 'hevy',
        label: 'Hevy',
        state: hevyState,
        message: hevyState === 'ready' ? 'Hevy is up to date.' : 'Hevy was last synced 6 days ago.',
        action_label: null,
        action_href: null,
      },
    },
  });

  async function open(status: object | 'error') {
    await TestBed.configureTestingModule({
      imports: [Chat],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    const http = TestBed.inject(HttpTestingController);
    const fixture = TestBed.createComponent(Chat);
    fixture.detectChanges();
    http.expectOne('/api/chat/history').flush({ messages: [] });
    const request = http.expectOne('/api/integrations/status');
    if (status === 'error') request.flush({}, { status: 500, statusText: 'Server Error' });
    else request.flush(status);
    await fixture.whenStable();
    fixture.detectChanges();
    return { fixture, http, el: fixture.nativeElement as HTMLElement };
  }

  afterEach(() => vi.unstubAllGlobals());

  it('tells the user when the AI provider is not set up, with a link to fix it', async () => {
    const { el } = await open(account('setup_required', 'ready'));
    const notice = el.querySelector('.status-notice[data-connector="ai:claude"]');
    expect(notice?.getAttribute('role')).toBe('alert');
    expect(notice?.textContent).toContain('is not set up yet');
    expect(notice?.querySelector('a')?.getAttribute('href')).toBe('/settings?section=ai');
  });

  it('warns that Coach is working from stale workout data', async () => {
    const { el } = await open(account('ready', 'stale'));
    expect(el.querySelector('.status-notice[data-connector="hevy"]')?.textContent).toContain(
      'last synced 6 days ago',
    );
    expect(el.querySelector('.status-notice[data-connector="ai:claude"]')).toBeNull();
  });

  it('shows no banner when everything is ready', async () => {
    const { el } = await open(account('ready', 'ready'));
    expect(el.querySelector('.status-notice')).toBeNull();
  });

  it('stays usable when readiness cannot be loaded', async () => {
    const { el, fixture } = await open('error');
    expect(el.querySelector('.status-notice')).toBeNull();
    expect(fixture.componentInstance.account()).toBeNull();
    expect(el.querySelector('textarea, input')).not.toBeNull();
  });

  it('refreshes readiness after a Coach attempt so the banner reflects the outcome', async () => {
    const { fixture, http } = await open(account('ready', 'ready'));
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(new Response('Fine.', { headers: { 'Content-Type': 'text/plain' } })),
    );
    fixture.componentInstance.userInput = 'How am I doing?';
    await fixture.componentInstance.sendMessage();
    http.expectOne('/api/integrations/status').flush(account('setup_required', 'ready'));
    fixture.detectChanges();
    expect(
      (fixture.nativeElement as HTMLElement).querySelector(
        '.status-notice[data-connector="ai:claude"]',
      ),
    ).not.toBeNull();
  });
});
