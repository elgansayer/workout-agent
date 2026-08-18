import { Component, inject, signal, OnInit } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';

@Component({
  selector: 'app-progress',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './progress.html',
  styleUrl: './progress.css'
})
export class Progress implements OnInit {
  private http = inject(HttpClient);
  private sanitizer = inject(DomSanitizer);

  data = signal<any>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);
  xaiReasoning = signal<{ [key: string]: string }>({});

  ngOnInit() {
    this.http.get('/api/progress').subscribe({
      next: (data: any) => {
        this.data.set(data);
        this.loading.set(false);
      },
      error: () => {
        this.error.set('Failed to load progress data');
        this.loading.set(false);
      }
    });
  }

  toggleXAI(exerciseName: string) {
    const current = this.xaiReasoning();
    if (current[exerciseName]) {
      const next = { ...current };
      delete next[exerciseName];
      this.xaiReasoning.set(next);
      return;
    }

    this.xaiReasoning.set({ ...current, [exerciseName]: 'Analyzing history...' });
    const today = new Date().toISOString().split('T')[0];
    const contextId = `${today}_${exerciseName}`;

    this.http.get(`/api/xai_reasoning/${encodeURIComponent(contextId)}`).subscribe({
      next: (res: any) => {
        this.xaiReasoning.set({
          ...this.xaiReasoning(),
          [exerciseName]: res.reasoning || 'No explanation available.'
        });
      },
      error: () => {
        this.xaiReasoning.set({
          ...this.xaiReasoning(),
          [exerciseName]: 'Error analyzing history.'
        });
      }
    });
  }

  safeHtml(html: string): SafeHtml {
    return this.sanitizer.bypassSecurityTrustHtml(html || '');
  }
}
