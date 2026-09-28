import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
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
    const fetchMock = vi.fn().mockResolvedValue(new Response('Good session.', {
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    }));
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
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: 'No AI provider key available. Add your own key in Settings.' }),
      { status: 400, headers: { 'Content-Type': 'application/json' } },
    )));
    chat.userInput = 'Review my training';

    await chat.sendMessage();

    expect(chat.error()).toContain('Add your own key in Settings');
    expect(chat.userInput).toBe('Review my training');
    expect(chat.messages().some(message => message.role === 'assistant')).toBe(false);
    expect(chat.isStreaming()).toBe(false);
  });

  it('does not show an HTML sign-in page as a successful coach reply', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>Sign in</html>', {
      headers: { 'Content-Type': 'text/html' },
    })));
    chat.userInput = 'Hello';

    await chat.sendMessage();

    expect(chat.error()).toContain('Sign in again');
    expect(chat.messages()).toHaveLength(1);
  });
});
