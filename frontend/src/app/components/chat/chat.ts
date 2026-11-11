import { Component, inject, signal, OnInit, OnDestroy, ViewChild, ElementRef } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { environment } from '../../../environments/environment';

interface EvidenceItem {
  evidence_id: string;
  source: string;
  record_id: string;
  date: string;
  freshness: string;
  transformation_version: string;
  summary: string;
}

interface ChatEvidence {
  user_id?: string;
  generated_at?: string;
  window_start?: string;
  window_end?: string;
  transformation_version?: string;
  grounding?: string;
  cited?: string[];
  items: EvidenceItem[];
}

const str = (v: unknown): string => (typeof v === 'string' ? v : '');

function parseEvidence(data: Record<string, unknown>): ChatEvidence {
  const rawItems = Array.isArray(data['items']) ? data['items'] : [];
  const items: EvidenceItem[] = [];
  for (const raw of rawItems) {
    if (typeof raw !== 'object' || raw === null) continue;
    const r = raw as Record<string, unknown>;
    items.push({
      evidence_id: str(r['evidence_id']),
      source: str(r['source']),
      record_id: str(r['record_id']),
      date: str(r['date']),
      freshness: str(r['freshness']),
      transformation_version: str(r['transformation_version']),
      summary: str(r['summary']),
    });
  }
  return {
    user_id: str(data['user_id']) || undefined,
    generated_at: str(data['generated_at']) || undefined,
    window_start: str(data['window_start']) || undefined,
    window_end: str(data['window_end']) || undefined,
    transformation_version: str(data['transformation_version']) || undefined,
    grounding: str(data['grounding']) || undefined,
    cited: Array.isArray(data['cited'])
      ? data['cited'].filter((c): c is string => typeof c === 'string')
      : undefined,
    items,
  };
}

interface ChatMessage {
  role: string;
  content: string;
  created_at?: string;
  evidence?: ChatEvidence;
}

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

  messages = signal<ChatMessage[]>([]);
  userInput = '';
  isStreaming = signal<boolean>(false);
  currentStreamText = signal<string>('');
  currentEvidence = signal<ChatEvidence | null>(null);
  currentGrounding = signal<string>('');
  error = signal<string | null>(null);
  private abortController: AbortController | null = null;

  ngOnInit() {
    this.loadChatHistory();
  }

  ngOnDestroy(): void {
    this.stopGeneration();
  }

  loadChatHistory() {
    this.http.get<{ messages: ChatMessage[] }>('/api/chat/history').subscribe({
      next: (res) => {
        const msgs = res.messages || (Array.isArray(res) ? res : []);
        this.messages.set(msgs);
        this.scrollToBottom();
      },
      error: () => this.error.set('Could not load chat history. Try refreshing the page.'),
    });
  }

  askSuggested(query: string) {
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
    if (!query || this.isStreaming()) return;

    this.userInput = '';
    this.isStreaming.set(true);
    this.currentStreamText.set('');
    this.currentEvidence.set(null);
    this.currentGrounding.set('');
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
      if (!response.headers.get('content-type')?.startsWith('text/event-stream')) {
        throw new Error('Coach returned an unexpected response. Sign in again and try once more.');
      }

      const reader = response.body?.getReader();
      const decoder = new TextDecoder();
      let fullText = '';
      let buffer = '';
      let cited: string[] = [];

      const handleEvent = (block: string) => {
        let event = 'message';
        const dataLines: string[] = [];
        for (const line of block.split(/\r?\n/)) {
          if (line.startsWith('event:')) event = line.slice(6).trim();
          else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
        }
        if (dataLines.length === 0) return;
        let data: Record<string, unknown>;
        try {
          data = JSON.parse(dataLines.join('\n')) as Record<string, unknown>;
        } catch {
          return;
        }
        if (event === 'evidence') {
          this.currentEvidence.set(parseEvidence(data));
        } else if (event === 'delta') {
          fullText += typeof data['text'] === 'string' ? data['text'] : '';
          this.currentStreamText.set(fullText);
          this.scrollToBottom();
        } else if (event === 'status') {
          this.currentGrounding.set(typeof data['grounding'] === 'string' ? data['grounding'] : '');
          cited = Array.isArray(data['cited']) ? data['cited'].filter((c): c is string => typeof c === 'string') : [];
        }
      };

      const drain = () => {
        let match = buffer.match(/\r?\n\r?\n/);
        while (match && match.index !== undefined) {
          const block = buffer.slice(0, match.index);
          buffer = buffer.slice(match.index + match[0].length);
          handleEvent(block);
          match = buffer.match(/\r?\n\r?\n/);
        }
      };

      if (reader) {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          drain();
        }
      }

      buffer += decoder.decode();
      drain();
      if (buffer.trim()) handleEvent(buffer);

      if (!fullText.trim()) {
        throw new Error(
          'Coach returned no response. Check your AI provider in Settings and try again.',
        );
      }
      const evidence = this.currentEvidence();
      this.messages.set([
        ...this.messages(),
        {
          role: 'assistant',
          content: fullText,
          created_at: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
          evidence: evidence
            ? { ...evidence, grounding: this.currentGrounding(), cited }
            : undefined,
        },
      ]);
    } catch (err: unknown) {
      if (!(err instanceof Error && err.name === 'AbortError')) {
        this.error.set(err instanceof Error ? err.message : 'Could not reach Coach. Try again.');
        if (!this.userInput) this.userInput = query;
      }
    } finally {
      this.isStreaming.set(false);
      this.currentStreamText.set('');
      this.currentEvidence.set(null);
      this.currentGrounding.set('');
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

  groundingLabel(status?: string): string {
    switch (status) {
      case 'observed':
        return 'Grounded in your records';
      case 'inferred':
        return 'Inferred — no direct citation';
      case 'incomplete':
        return 'Incomplete evidence';
      case 'pending':
        return 'Evidence attached — verifying citations';
      default:
        return 'Unknown — no matching records';
    }
  }

  groundingClass(status?: string): string {
    return `evidence-badge ${status || 'unknown'}`;
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
