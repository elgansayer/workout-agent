import { Component, inject, signal, OnInit, ViewChild, ElementRef } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { environment } from '../../../environments/environment';

@Component({
  selector: 'app-chat',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './chat.html',
  styleUrl: './chat.css'
})
export class Chat implements OnInit {
  private http = inject(HttpClient);
  private sanitizer = inject(DomSanitizer);

  @ViewChild('scrollContainer') private scrollContainer?: ElementRef;

  messages = signal<any[]>([]);
  userInput = '';
  isStreaming = signal<boolean>(false);
  currentStreamText = signal<string>('');
  error = signal<string | null>(null);
  private abortController: AbortController | null = null;

  ngOnInit() {
    this.loadChatHistory();
  }

  loadChatHistory() {
    this.http.get<{ messages: any[] }>('/api/chat/history').subscribe({
      next: (res) => {
        const msgs = res.messages || (Array.isArray(res) ? res : []);
        this.messages.set(msgs);
        this.scrollToBottom();
      },
      error: () => {}
    });
  }

  askSuggested(query: string) {
    this.userInput = query;
    this.sendMessage();
  }

  onKeyDown(event: KeyboardEvent) {
    if (event.key === 'Enter' && !event.shiftKey) {
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
    this.error.set(null);

    const userMsg = { role: 'user', content: query, created_at: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) };
    this.messages.set([...this.messages(), userMsg]);
    this.scrollToBottom();

    this.abortController = new AbortController();

    try {
      const baseUrl = environment.apiUrl.replace(/\/$/, '');
      const response = await fetch(`${baseUrl}/api/rag_search?q=${encodeURIComponent(query)}`, {
        signal: this.abortController.signal,
        credentials: 'include',
        cache: 'no-store'
      });

      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        const message = response.status === 401
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
        throw new Error('Coach returned no response. Check your AI provider in Settings and try again.');
      }
      this.messages.set([
        ...this.messages(),
        {
          role: 'assistant',
          content: fullText,
          created_at: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        }
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
    if (!confirm('Clear all chat history?')) return;
    this.http.post('/api/chat/clear', {}).subscribe({
      next: () => {
        this.messages.set([]);
      }
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
    html = paragraphs.map(p => {
      p = p.trim();
      if (!p) return '';
      const lines = p.split('\n');
      if (lines.every(l => /^\s*[-•]\s/.test(l))) {
        return '<ul>' + lines.map(l => '<li>' + l.replace(/^\s*[-•]\s/, '') + '</li>').join('') + '</ul>';
      }
      return '<p>' + p.replace(/\n/g, '<br>') + '</p>';
    }).join('');

    return this.sanitizer.bypassSecurityTrustHtml(html);
  }

  private scrollToBottom() {
    setTimeout(() => {
      if (this.scrollContainer) {
        this.scrollContainer.nativeElement.scrollTop = this.scrollContainer.nativeElement.scrollHeight;
      }
    }, 50);
  }
}
