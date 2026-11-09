import { PagedList } from '../../shared/paged-list';
import { ListControls } from '../../shared/list-controls';
import { HlmCardImports } from '@spartan-ng/ui/card';
import { HlmBadgeImports } from '@spartan-ng/ui/badge';
import { HlmAlertImports } from '@spartan-ng/ui/alert';
import { Component, inject, signal, computed, OnInit } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { FormsModule } from '@angular/forms';
import { SetupNotice } from '../../shared/setup-notice';
import { SetupState, ViewState, resolveViewState } from '../../shared/setup-state';

@Component({
  selector: 'app-history',
  standalone: true,
  imports: [
    ListControls,
    CommonModule,
    FormsModule,
    HlmCardImports,
    HlmBadgeImports,
    HlmAlertImports,
    SetupNotice,
  ],
  templateUrl: './history.html',
  styleUrl: './history.css',
})
export class History implements OnInit {
  private http = inject(HttpClient);
  protected sanitizer = inject(DomSanitizer);

  data = signal<any>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);
  setup = signal<SetupState | null>(null);

  readonly hasData = computed(() => (this.data()?.logs?.length ?? 0) > 0);

  readonly viewState = computed<ViewState>(() =>
    resolveViewState({
      loading: this.loading(),
      error: this.error(),
      hasData: this.hasData(),
      stale: this.setup()?.stale ?? false,
    }),
  );

  readonly logs = new PagedList<any>(
    () => this.data()?.logs ?? [],
    (item) => `${item.focus} ${item.date} ${item.plan || ''}`,
    8,
  );

  ngOnInit() {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.error.set(null);
    this.http.get('/api/history').subscribe({
      next: (data: any) => {
        this.data.set(data);
        this.setup.set(data?.setup ?? null);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set('Failed to load history data');
        this.loading.set(false);
      },
    });
  }

  safeHtml(html: string): SafeHtml {
    if (!html) return '';
    return this.sanitizer.bypassSecurityTrustHtml(html);
  }
}
