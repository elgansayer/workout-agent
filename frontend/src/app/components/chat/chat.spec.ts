import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
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

  it('disables data-grounded prompts and displays setup explanation when prerequisites fail', () => {
    chat.capabilities.set({
      provider_ready: true,
      provider_name: 'gemini',
      capabilities: {
        workout_analysis: {
          enabled: false,
          status: 'missing_data',
          explanation: 'No workout logs found. Connect Hevy in Settings and sync your workout history.',
        },
        recovery_analysis: {
          enabled: false,
          status: 'missing_source',
          explanation: 'Connect a health source in Settings to enable recovery-aware coaching.',
        },
      },
      suggested_prompts: [
        {
          text: 'How is my deadlift progressing?',
          capability: 'workout_analysis',
          icon: '📊',
          enabled: false,
          explanation: 'No workout logs found. Connect Hevy in Settings and sync your workout history.',
        },
      ],
      overall_status: 'educational_only',
      disclaimer: 'Coach provides general educational guidance until training and recovery sources are connected.',
      welcome_message: 'Coach is in educational mode.',
    });

    const sendSpy = vi.spyOn(chat, 'sendMessage');
    chat.askSuggested('How is my deadlift progressing?');

    expect(sendSpy).not.toHaveBeenCalled();
    expect(chat.error()).toBe('No workout logs found. Connect Hevy in Settings and sync your workout history.');
  });

  it('enables data-grounded prompts when prerequisites pass', () => {
    chat.capabilities.set({
      provider_ready: true,
      provider_name: 'gemini',
      capabilities: {
        workout_analysis: {
          enabled: true,
          status: 'ready',
          explanation: 'Workout logs and personal records are connected and current.',
        },
      },
      suggested_prompts: [
        {
          text: 'How is my deadlift progressing?',
          capability: 'workout_analysis',
          icon: '📊',
          enabled: true,
          explanation: 'Workout logs and personal records are connected and current.',
        },
      ],
      overall_status: 'partially_grounded',
      disclaimer: 'Coach answers based on your connected training logs.',
      welcome_message: 'Coach has access to your training logs.',
    });

    const sendSpy = vi.spyOn(chat, 'sendMessage').mockResolvedValue();
    chat.askSuggested('How is my deadlift progressing?');

    expect(sendSpy).toHaveBeenCalledOnce();
    expect(chat.userInput).toBe('How is my deadlift progressing?');
    expect(chat.error()).toBeNull();
  });

  it('loads coaching capabilities from /api/coach/capabilities', () => {
    const http = TestBed.inject(HttpTestingController);
    chat.loadCapabilities();
    const req = http.expectOne('/api/coach/capabilities');
    expect(req.request.method).toBe('GET');
    req.flush({
      provider_ready: true,
      provider_name: 'gemini',
      overall_status: 'educational_only',
      capabilities: {},
      suggested_prompts: [],
      disclaimer: 'Educational disclaimer',
      welcome_message: 'Welcome message',
    });
    expect(chat.capabilities()?.overall_status).toBe('educational_only');
  });
});

describe('Coach capability copy', () => {
  const emptySource = { available: false, freshness: 'never', last_observed_at: null };

  function capability(overrides: Record<string, unknown> = {}): any {
    return {
      provider_name: 'gemini',
      provider_configured: false,
      provider_state: 'missing_key',
      can_chat: false,
      status: 'unconfigured',
      sync_configured: false,
      sync_state: 'not_configured',
      programme_available: false,
      has_training_data: false,
      has_fresh_training_data: false,
      sources: {
        training_logs: { ...emptySource },
        progress_history: { ...emptySource },
        personal_records: { ...emptySource },
        body_metrics: { ...emptySource },
      },
      ...overrides,
    };
  }

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Chat],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
  });

  async function render(capabilityPayload: ReturnType<typeof capability>) {
    const http = TestBed.inject(HttpTestingController);
    const fixture = TestBed.createComponent(Chat);
    fixture.detectChanges();
    http.expectOne('/api/chat/history').flush({ messages: [] });
    http.expectOne('/api/coach/capability').flush(capabilityPayload);
    await fixture.whenStable();
    fixture.detectChanges();
    return { fixture, element: fixture.nativeElement as HTMLElement, http };
  }

  function suggestButton(element: HTMLElement, text: string): HTMLButtonElement {
    const button = Array.from(
      element.querySelectorAll<HTMLButtonElement>('.suggest-card'),
    ).find((item) => item.textContent?.includes(text));
    if (!button) throw new Error(`Missing suggestion: ${text}`);
    return button;
  }

  it('claims no data or provider access when neither exists', async () => {
    const { element } = await render(capability());
    const welcome = element.querySelector('.welcome-sub')?.textContent ?? '';

    expect(element.querySelector('.status-text')?.textContent).toContain(
      'No data source or AI provider configured',
    );
    expect(welcome).not.toMatch(/training logs|body composition|personal records|history/i);
    expect(element.querySelector('.chat-disclaimer')?.textContent).not.toMatch(
      /training logs|recovery data|programme/i,
    );
    expect(
      Array.from(element.querySelectorAll<HTMLButtonElement>('.suggest-card')).every(
        (button) => button.disabled,
      ),
    ).toBe(true);
    expect(
      element.querySelector<HTMLButtonElement>('.chat-send-btn')?.disabled,
    ).toBe(true);
  });

  it('does not claim training data when a provider exists but nothing is synced', async () => {
    const { fixture, element } = await render(
      capability({
        provider_configured: true,
        provider_state: 'ready',
        can_chat: true,
        status: 'no_data',
      }),
    );
    const welcome = element.querySelector('.welcome-sub')?.textContent ?? '';

    expect(element.querySelector('.status-text')?.textContent).toContain(
      'No training data synced',
    );
    expect(welcome).toContain('No training data has been synced yet');
    expect(welcome).not.toContain('full access');
    expect(
      Array.from(element.querySelectorAll<HTMLButtonElement>('.suggest-card')).every(
        (button) => button.disabled,
      ),
    ).toBe(true);

    const input = element.querySelector<HTMLTextAreaElement>('.chat-textarea');
    if (!input) throw new Error('Missing chat input');
    input.value = 'Hello';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    expect(element.querySelector<HTMLButtonElement>('.chat-send-btn')?.disabled).toBe(false);
  });

  it('lists available data but blocks questions when no provider is configured', async () => {
    const { element } = await render(
      capability({
        has_training_data: true,
        has_fresh_training_data: true,
        status: 'provider_missing',
        sources: {
          training_logs: emptySource,
          progress_history: { available: true, freshness: 'fresh', last_observed_at: '2026-08-18' },
          personal_records: { available: true, freshness: 'fresh', last_observed_at: '2026-08-18' },
          body_metrics: { available: true, freshness: 'fresh', last_observed_at: '2026-08-19' },
        },
      }),
    );
    const welcome = element.querySelector('.welcome-sub')?.textContent ?? '';

    expect(element.querySelector('.status-text')?.textContent).toContain(
      'AI provider not configured',
    );
    expect(welcome).toContain('exercise progress');
    expect(welcome).toContain('body composition');
    expect(welcome).toContain('needs an AI provider');
    expect(welcome).not.toContain('full access');
    expect(
      Array.from(element.querySelectorAll<HTMLButtonElement>('.suggest-card')).every(
        (button) => button.disabled,
      ),
    ).toBe(true);
  });

  it('enables only the questions backed by available data', async () => {
    const { element } = await render(
      capability({
        provider_configured: true,
        provider_state: 'ready',
        can_chat: true,
        status: 'ready',
        sync_configured: true,
        sync_state: 'synced',
        has_training_data: true,
        has_fresh_training_data: true,
        sources: {
          training_logs: emptySource,
          progress_history: { available: true, freshness: 'fresh', last_observed_at: '2026-08-18' },
          personal_records: { available: true, freshness: 'fresh', last_observed_at: '2026-08-18' },
          body_metrics: emptySource,
        },
      }),
    );

    expect(element.querySelector('.status-text')?.textContent).toContain(
      'Ready to use your training data',
    );
    expect(suggestButton(element, 'deadlift progressing').disabled).toBe(false);
    expect(suggestButton(element, 'lifts are stalling').disabled).toBe(false);
    expect(suggestButton(element, 'recovering well enough').disabled).toBe(true);
    expect(suggestButton(element, 'body composition trending').disabled).toBe(true);
    expect(element.querySelector('.welcome-sub')?.textContent).not.toContain('full access');
  });

  it('reports stale training data without hiding the available sources', async () => {
    const { element } = await render(
      capability({
        provider_configured: true,
        provider_state: 'ready',
        can_chat: true,
        status: 'stale_data',
        sync_configured: true,
        sync_state: 'synced',
        has_training_data: true,
        has_fresh_training_data: false,
        sources: {
          training_logs: emptySource,
          progress_history: { available: true, freshness: 'stale', last_observed_at: '2026-06-01' },
          personal_records: { available: true, freshness: 'stale', last_observed_at: '2026-06-01' },
          body_metrics: emptySource,
        },
      }),
    );

    expect(element.querySelector('.status-text')?.textContent).toContain(
      'Training data is stale',
    );
    expect(element.querySelector('.welcome-sub')?.textContent).toContain(
      'Some data may be out of date',
    );
  });

  it('fails closed when capability cannot be verified', async () => {
    const { element } = await render(
      capability({
        status: 'unavailable',
        provider_name: null,
        provider_state: 'unknown',
      }),
    );
    const welcome = element.querySelector('.welcome-sub')?.textContent ?? '';

    expect(element.querySelector('.status-text')?.textContent).toContain(
      'Could not verify data access',
    );
    expect(welcome).toContain('will not claim access');
    expect(
      Array.from(element.querySelectorAll<HTMLButtonElement>('.suggest-card')).every(
        (button) => button.disabled,
      ),
    ).toBe(true);
  });
});
