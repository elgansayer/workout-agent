import { Component, inject, signal, OnInit, OnDestroy, ViewChild, ElementRef } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { environment } from '../../../environments/environment';

export interface CapabilityItem {
  enabled: boolean;
  status: string;
  explanation: string;
}

export interface SuggestedPromptState {
  text: string;
  capability: string;
  icon: string;
  enabled: boolean;
  explanation: string;
}

export interface CoachingCapabilityState {
  provider_ready: boolean;
  provider_name: string | null;
  capabilities: Record<string, CapabilityItem>;
  suggested_prompts: SuggestedPromptState[];
  overall_status: 'setup_required' | 'educational_only' | 'partially_grounded' | 'fully_grounded';
  disclaimer: string;
  welcome_message: string;
}

interface CoachSourceCapability {
  available: boolean;
  freshness: string;
  last_observed_at: string | null;
}

interface CoachCapability {
  provider_name: string | null;
  provider_configured: boolean;
  provider_state: string;
  can_chat: boolean;
  status: string;
  sync_configured: boolean;
  sync_state: string;
  programme_available: boolean;
  has_training_data: boolean;
  has_fresh_training_data: boolean;
  sources: Record<string, CoachSourceCapability>;
}

interface CoachSuggestion {
  icon: string;
  text: string;
  requires: string[];
  enabled?: boolean;
}

const CAPABILITY_UNAVAILABLE: CoachCapability = {
  provider_name: null,
  provider_configured: false,
  provider_state: 'unknown',
  can_chat: false,
  status: 'unavailable',
  sync_configured: false,
  sync_state: 'unknown',
  programme_available: false,
  has_training_data: false,
  has_fresh_training_data: false,
  sources: {},
};

const SUGGESTIONS: CoachSuggestion[] = [
  { icon: '📊', text: 'How is my deadlift progressing?', requires: ['progress_history'] },
  { icon: '💤', text: 'Am I recovering well enough?', requires: ['body_metrics'] },
  { icon: '📉', text: 'Which lifts are stalling?', requires: ['progress_history'] },
  {
    icon: '🔄',
    text: 'Should I deload this week?',
    requires: ['training_logs', 'progress_history', 'programme'],
  },
  {
    icon: '⚖️',
    text: 'How is my body composition trending?',
    requires: ['body_metrics'],
  },
  {
    icon: '🎯',
    text: 'What should I focus on this block?',
    requires: ['programme', 'progress_history', 'training_logs'],
  },
];
@Component({
  selector: 'app-chat',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './chat.html',
  styleUrl: './chat.css',
})
export class Chat implements OnInit, OnDestroy {
  private http = inject(HttpClient);
  private sanitizer = inject(DomSanitizer);

  @ViewChild('scrollContainer') private scrollContainer?: ElementRef;

  messages = signal<any[]>([]);
  userInput = '';
  isStreaming = signal<boolean>(false);
  currentStreamText = signal<string>('');
  error = signal<string | null>(null);
  capabilities = signal<CoachingCapabilityState | null>(null);
  capability = signal<CoachCapability | null>(null);
  private abortController: AbortController | null = null;

  private defaultSuggestedPrompts: SuggestedPromptState[] = [
    {
      text: 'How is my deadlift progressing?',
      capability: 'workout_analysis',
      icon: '📊',
      enabled: false,
      explanation: 'Checking training prerequisites...',
    },
    {
      text: 'Am I recovering well enough?',
      capability: 'recovery_analysis',
      icon: '💤',
      enabled: false,
      explanation: 'Checking recovery prerequisites...',
    },
    {
      text: 'Which lifts are stalling?',
      capability: 'workout_analysis',
      icon: '📉',
      enabled: false,
      explanation: 'Checking training prerequisites...',
    },
    {
      text: 'Should I deload this week?',
      capability: 'workout_analysis',
      icon: '🔄',
      enabled: false,
      explanation: 'Checking training prerequisites...',
    },
    {
      text: 'How is my body composition trending?',
      capability: 'body_composition',
      icon: '⚖️',
      enabled: false,
      explanation: 'Checking body composition prerequisites...',
    },
    {
      text: 'What should I focus on this block?',
      capability: 'programme_guidance',
      icon: '🎯',
      enabled: false,
      explanation: 'Checking programme prerequisites...',
    },
  ];

  ngOnInit() {
    this.loadCapabilities();
    this.loadChatHistory();
    this.loadCapability();
  }

  ngOnDestroy(): void {
    this.stopGeneration();
  }

  loadCapabilities() {
    this.http.get<CoachingCapabilityState>('/api/coach/capabilities').subscribe({
      next: (caps) => {
        this.capabilities.set(caps);
      },
      error: () => {
        // Leave capabilities as fallback
      },
    });
  }

  suggestedPrompts(): SuggestedPromptState[] {
    return this.capabilities()?.suggested_prompts || this.defaultSuggestedPrompts;
  }

  loadChatHistory() {
    this.http.get<{ messages: any[] }>('/api/chat/history').subscribe({
      next: (res) => {
        const msgs = res.messages || (Array.isArray(res) ? res : []);
        this.messages.set(msgs);
        this.scrollToBottom();
      },
      error: () => this.error.set('Could not load chat history. Try refreshing the page.'),
    });
  }

  loadCapability() {
    this.http.get<CoachCapability>('/api/coach/capability').subscribe({
      next: (capability) => this.capability.set(capability),
      error: () => this.capability.set(CAPABILITY_UNAVAILABLE),
    });
  }

  statusText(): string {
    if (this.isStreaming()) return 'Responding...';
    if (this.error()) return 'Could not connect';
    const capability = this.capability();
    if (!capability) return 'Checking data access...';
    switch (capability.status) {
      case 'ready':
        return capability.has_fresh_training_data
          ? 'Ready to use your training data'
          : 'Ready, but your training data is stale';
      case 'stale_data':
        return 'Training data is stale';
      case 'no_data':
        return 'No training data synced';
      case 'provider_missing':
        return 'AI provider not configured';
      case 'unconfigured':
        return 'No data source or AI provider configured';
      default:
        return 'Could not verify data access';
    }
  }

  welcomeSummary(): string {
    const capability = this.capability();
    if (!capability) return 'Checking which data sources Coach can use...';
    if (capability.status === 'unavailable') {
      return 'Coach could not verify which data sources are available, so it will not claim access to any.';
    }

    const sources = this.availableSourceLabels();
    if (!capability.provider_configured) {
      if (sources.length > 0) {
        return `This account has ${this.joinLabels(sources)} on file, but Coach needs an AI provider configured in Settings before it can answer.`;
      }
      return 'Connect a data source and an AI provider in Settings to start coaching.';
    }

    if (!capability.has_training_data) {
      if (sources.length === 0) {
        return 'No training data has been synced yet, so Coach cannot answer about your logs, records, or history. Connect Hevy in Settings or sync your history.';
      }
      return `Coach can use your ${this.joinLabels(sources)}. No training logs, records, or history have been synced yet.`;
    }

    const freshness = capability.has_fresh_training_data
      ? ''
      : ' Some data may be out of date; sync to refresh it.';
    return `Coach can use your ${this.joinLabels(sources)}.${freshness}`;
  }

  inputHint(): string {
    const capability = this.capability();
    if (!capability) return 'Checking which data sources Coach can use...';
    if (!capability.provider_configured) {
      return 'Configure an AI provider in Settings to message Coach...';
    }
    if (!capability.has_training_data) {
      return 'Ask a general fitness question...';
    }
    return 'Ask Coach about your training, recovery, or programme...';
  }

  disclaimerText(): string {
    const capability = this.capability();
    if (!capability) return 'Checking which sources Coach can use...';
    if (!capability.provider_configured) {
      return 'Coach cannot answer until an AI provider is configured in Settings.';
    }
    if (!capability.has_training_data) {
      if (this.availableSourceLabels().length === 0) {
        return 'Coach has no training data to reference. General fitness questions are still available.';
      }
      return 'Coach answers only from the sources listed above; no training logs, records, or history are available.';
    }
    return 'Coach answers only from the sources listed above; it cannot see unsynced data.';
  }

  suggestions(): CoachSuggestion[] {
    const capability = this.capability();
    const capabilities = this.capabilities();
    return SUGGESTIONS.map((suggestion) => {
      let enabled =
        !!capability &&
        capability.can_chat &&
        suggestion.requires.some((source) => this.sourceAvailable(source));
      if (capabilities) {
        const promptState = capabilities.suggested_prompts?.find((p) => p.text === suggestion.text);
        if (promptState !== undefined) {
          enabled = enabled && promptState.enabled;
        }
      }
      return {
        ...suggestion,
        enabled,
      };
    });
  }

  getSuggestionExplanation(text: string): string | null {
    if (this.capabilities()) {
      const prompt = this.suggestedPrompts().find((p) => p.text === text);
      if (prompt && !prompt.enabled) {
        return prompt.explanation;
      }
    }
    return null;
  }

  canSend(): boolean {
    const capability = this.capability();
    return !this.isStreaming() && (!capability || capability.can_chat);
  }

  private availableSourceLabels(): string[] {
    const capability = this.capability();
    if (!capability) return [];
    const labels: string[] = [];
    if (capability.sources['training_logs']?.available) labels.push('training logs');
    if (capability.sources['progress_history']?.available) labels.push('exercise progress');
    if (capability.sources['personal_records']?.available) labels.push('personal records');
    if (capability.sources['body_metrics']?.available) labels.push('body composition');
    if (capability.programme_available) labels.push('active programme');
    return labels;
  }

  private sourceAvailable(source: string): boolean {
    const capability = this.capability();
    if (!capability) return false;
    if (source === 'programme') return capability.programme_available;
    return !!capability.sources[source]?.available;
  }

  private joinLabels(labels: string[]): string {
    if (labels.length <= 1) return labels[0] || '';
    return `${labels.slice(0, -1).join(', ')} and ${labels[labels.length - 1]}`;
  }

  askSuggested(query: string) {
    const prompt = this.capabilities()
      ? this.suggestedPrompts().find((p) => p.text === query)
      : undefined;
    if (prompt) {
      if (!prompt.enabled) {
        this.error.set(prompt.explanation);
        return;
      }
    } else {
      const suggestion = this.suggestions().find((item) => item.text === query);
      if (suggestion && !suggestion.enabled) return;
    }
    this.userInput = query;
    this.sendMessage();
  }

  onKeyDown(event: KeyboardEvent) {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      this.sendMessage();
    }
  }

  async sendMessage() {
    const query = this.userInput.trim();
    const capability = this.capability();
    if (!query || this.isStreaming() || (capability && !capability.can_chat)) return;

    this.userInput = '';
    this.isStreaming.set(true);
    this.currentStreamText.set('');
    this.error.set(null);

    const userMsg = {
      role: 'user',
      content: query,
      created_at: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    };
    this.messages.set([...this.messages(), userMsg]);
    this.scrollToBottom();

    this.abortController = new AbortController();

    try {
      const baseUrl = environment.apiUrl.replace(/\/$/, '');
      const response = await fetch(`${baseUrl}/api/rag_search?q=${encodeURIComponent(query)}`, {
        signal: this.abortController.signal,
        credentials: 'include',
        cache: 'no-store',
      });

      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        const message =
          response.status === 401
            ? 'Your session has expired. Sign in again to use Coach.'
            : response.status === 429
              ? 'Too many requests. Wait a minute, then try again.'
              : typeof payload?.detail === 'string'
                ? payload.detail
                : 'Coach could not connect. Check your AI provider in Settings and try again.';
        throw new Error(message);
      }
      if (!response.headers.get('content-type')?.startsWith('text/plain')) {
        throw new Error('Coach returned an unexpected response. Sign in again and try once more.');
      }

      const reader = response.body?.getReader();
      const decoder = new TextDecoder();
      let fullText = '';

      if (reader) {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          const chunk = decoder.decode(value, { stream: true });
          fullText += chunk;
          this.currentStreamText.set(fullText);
          this.scrollToBottom();
        }
      }

      fullText += decoder.decode();
      if (!fullText.trim()) {
        throw new Error(
          'Coach returned no response. Check your AI provider in Settings and try again.',
        );
      }
      this.messages.set([
        ...this.messages(),
        {
          role: 'assistant',
          content: fullText,
          created_at: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        },
      ]);
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        this.error.set(err instanceof Error ? err.message : 'Could not reach Coach. Try again.');
        if (!this.userInput) this.userInput = query;
      }
    } finally {
      this.isStreaming.set(false);
      this.currentStreamText.set('');
      this.abortController = null;
      this.scrollToBottom();
    }
  }

  stopGeneration() {
    if (this.abortController) {
      this.abortController.abort();
    }
  }

  clearChat() {
    if (this.isStreaming() || !confirm('Clear all chat history?')) return;
    this.http.post('/api/chat/clear', {}).subscribe({
      next: () => {
        this.messages.set([]);
      },
      error: () => this.error.set('Could not clear chat history. Please try again.'),
    });
  }

  renderMarkdown(text: string): SafeHtml {
    if (!text) return '';
    let html = text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/__(.+?)__/g, '<strong>$1</strong>')
      .replace(/\*(?!\s)(.+?)(?<!\s)\*/g, '<em>$1</em>')
      .replace(/`([^`]+)`/g, '<code>$1</code>');

    const paragraphs = html.split(/\n{2,}/);
    html = paragraphs
      .map((p) => {
        p = p.trim();
        if (!p) return '';
        const lines = p.split('\n');
        if (lines.every((l) => /^\s*[-•]\s/.test(l))) {
          return (
            '<ul>' +
            lines.map((l) => '<li>' + l.replace(/^\s*[-•]\s/, '') + '</li>').join('') +
            '</ul>'
          );
        }
        return '<p>' + p.replace(/\n/g, '<br>') + '</p>';
      })
      .join('');

    return this.sanitizer.bypassSecurityTrustHtml(html);
  }

  private scrollToBottom() {
    setTimeout(() => {
      if (this.scrollContainer) {
        this.scrollContainer.nativeElement.scrollTop =
          this.scrollContainer.nativeElement.scrollHeight;
      }
    }, 50);
  }
}
