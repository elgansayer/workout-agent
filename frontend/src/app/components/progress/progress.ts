import { PagedList } from '../../shared/paged-list';
import { ListControls } from '../../shared/list-controls';
import { ReadinessBanner } from '../../shared/readiness-banner';
import { Component, inject, signal, OnInit } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';

@Component({
  selector: 'app-progress',
  standalone: true,
  imports: [ListControls, CommonModule, ReadinessBanner],
  templateUrl: './progress.html',
  styleUrl: './progress.css',
})
export class Progress implements OnInit {
  private http = inject(HttpClient);
  private sanitizer = inject(DomSanitizer);

  data = signal<any>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);
  xaiReasoning = signal<{ [key: string]: string }>({});

  readonly lifts = new PagedList<any>(
    () => this.data()?.charts ?? [],
    (item) => item.name,
    6,
  );

  ngOnInit() {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.error.set(null);
    this.http.get('/api/progress').subscribe({
      next: (data: any) => {
        this.data.set(data);
        this.loading.set(false);
      },
      error: () => {
        this.error.set('Failed to load progress data');
        this.loading.set(false);
      },
    });
  }

  private readonly explanationRequests = new Map<string, number>();

  toggleXAI(exerciseName: string) {
    const request = (this.explanationRequests.get(exerciseName) ?? 0) + 1;
    this.explanationRequests.set(exerciseName, request);
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
        if (this.explanationRequests.get(exerciseName) !== request) return;
        this.xaiReasoning.set({
          ...this.xaiReasoning(),
          [exerciseName]: res.reasoning || 'No explanation available.',
        });
      },
      error: () => {
        if (this.explanationRequests.get(exerciseName) !== request) return;
        this.xaiReasoning.set({
          ...this.xaiReasoning(),
          [exerciseName]: 'Error analyzing history.',
        });
      },
    });
  }

  safeHtml(html: string): SafeHtml {
    return this.sanitizer.bypassSecurityTrustHtml(html || '');
  }
}
