import { Component, inject, signal, OnInit, ViewChild, ElementRef } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';

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

    const userMsg = { role: 'user', content: query, created_at: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) };
    this.messages.set([...this.messages(), userMsg]);
    this.scrollToBottom();

    this.abortController = new AbortController();

    try {
      const response = await fetch(`/api/rag_search?q=${encodeURIComponent(query)}`, {
        signal: this.abortController.signal
      });

      if (!response.ok) {
        this.messages.set([
          ...this.messages(),
          { role: 'assistant', content: 'Sorry, I encountered an error connecting to Coach. Please try again.' }
        ]);
        this.isStreaming.set(false);
        this.scrollToBottom();
        return;
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
        this.messages.set([
          ...this.messages(),
          { role: 'assistant', content: 'Sorry, something went wrong while streaming the response.' }
        ]);
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
