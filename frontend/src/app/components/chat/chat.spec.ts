import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { vi } from 'vitest';
import { environment } from '../../../environments/environment';
import { Chat } from './chat';

function sse(...events: Array<[string, unknown]>): string {
  return events
    .map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
    .join('');
}

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

  it('streams evidence and the reply from the configured API origin', async () => {
    environment.apiUrl = 'https://api.example.test/';
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        sse(
          [
            'evidence',
            {
              window_start: '2026-03-01',
              window_end: '2026-03-08',
              transformation_version: 'coaching-context-v1',
              items: [
                {
                  evidence_id: 'E1',
                  source: 'exercise_progress',
                  record_id: 'exercise_progress:1',
                  date: '2026-03-02',
                  freshness: 'fresh',
                  transformation_version: 'exercise-progress-v1',
                  summary: 'Deadlift top set 140kg x 5 reps x 4 sets',
                },
              ],
            },
          ],
          ['delta', { text: 'Good session [E1].' }],
          ['status', { grounding: 'observed', cited: ['E1'], evidence_count: 1 }],
          ['done', {}],
        ),
        { headers: { 'Content-Type': 'text/event-stream; charset=utf-8' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    chat.userInput = 'How am I doing?';

    await chat.sendMessage();

    expect(fetchMock).toHaveBeenCalledWith(
      'https://api.example.test/api/rag_search?q=How%20am%20I%20doing%3F',
      expect.objectContaining({ credentials: 'include', cache: 'no-store' }),
    );
    const reply = chat.messages().at(-1);
    expect(reply?.content).toBe('Good session [E1].');
    expect(reply?.evidence?.grounding).toBe('observed');
    expect(reply?.evidence?.items?.[0]?.record_id).toBe('exercise_progress:1');
    expect(chat.error()).toBeNull();
    expect(chat.isStreaming()).toBe(false);
  });

  it('labels replies without cited evidence as inferred', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          new Response(
            sse(
              ['evidence', { items: [], window_start: '2026-03-01', window_end: '2026-03-08' }],
              ['delta', { text: 'You should deload.' }],
              ['status', { grounding: 'inferred', cited: [], evidence_count: 1 }],
              ['done', {}],
            ),
            { headers: { 'Content-Type': 'text/event-stream' } },
          ),
        ),
    );
    chat.userInput = 'Should I deload?';

    await chat.sendMessage();

    expect(chat.messages().at(-1)?.evidence?.grounding).toBe('inferred');
    expect(chat.groundingLabel('inferred')).toContain('Inferred');
    expect(chat.groundingLabel('unknown')).toContain('Unknown');
  });

  it('shows configuration errors and keeps the question available to retry', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
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
