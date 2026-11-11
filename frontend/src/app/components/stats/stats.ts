import { PagedList } from '../../shared/paged-list';
import { ListControls } from '../../shared/list-controls';
import { HlmCardImports } from '@spartan-ng/ui/card';
import { HlmAlertImports } from '@spartan-ng/ui/alert';
import { Component, inject, signal, computed, OnInit } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { FormsModule } from '@angular/forms';
import { LocalDatePipe, LocalNumberPipe } from '../../shared/local-format.pipes';
import { SetupNotice } from '../../shared/setup-notice';
import {
  SetupState,
  ViewState,
  needsAttention,
  resolveViewState,
} from '../../shared/setup-state';

@Component({
  selector: 'app-stats',
  standalone: true,
  imports: [
    ListControls,
    CommonModule,
    FormsModule,
    HlmCardImports,
    HlmAlertImports,
    LocalDatePipe,
    LocalNumberPipe,
    SetupNotice,
  ],
  templateUrl: './stats.html',
  styleUrl: './stats.css',
})
export class Stats implements OnInit {
  private http = inject(HttpClient);
  protected sanitizer = inject(DomSanitizer);

  data = signal<any>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);
  setup = signal<SetupState | null>(null);

  readonly hasData = computed(() => (this.data()?.total_sessions ?? 0) > 0);

  readonly viewState = computed<ViewState>(() =>
    resolveViewState({
      loading: this.loading(),
      error: this.error(),
      hasData: this.hasData(),
      attention: needsAttention(this.setup()),
    }),
  );

  readonly records = new PagedList<any>(
    () => this.data()?.prs ?? [],
    (item) => item.exercise,
    10,
  );

  ngOnInit() {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.error.set(null);
    this.http.get('/api/stats').subscribe({
      next: (data: any) => {
        this.data.set(data);
        this.setup.set(data?.setup ?? null);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set('Failed to load stats data');
        this.loading.set(false);
      },
    });
  }

  safeHtml(html: string): SafeHtml {
    if (!html) return '';
    return this.sanitizer.bypassSecurityTrustHtml(html);
  }
}
