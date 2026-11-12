import { PagedList } from '../../shared/paged-list';
import { ListControls } from '../../shared/list-controls';
import { HlmCardImports } from '@spartan-ng/ui/card';
import { HlmAlertImports } from '@spartan-ng/ui/alert';
import { Component, inject, signal, computed, OnInit } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { FormsModule } from '@angular/forms';
import { LocalDatePipe } from '../../shared/local-format.pipes';
import { SetupNotice } from '../../shared/setup-notice';
import {
  SetupState,
  ViewState,
  needsAttention,
  resolveViewState,
} from '../../shared/setup-state';

@Component({
  selector: 'app-checkins',
  standalone: true,
  imports: [
    ListControls,
    CommonModule,
    FormsModule,
    HlmCardImports,
    HlmAlertImports,
    LocalDatePipe,
    SetupNotice,
  ],
  templateUrl: './checkins.html',
  styleUrl: './checkins.css',
})
export class Checkins implements OnInit {
  private http = inject(HttpClient);
  protected sanitizer = inject(DomSanitizer);

  data = signal<any>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);
  setup = signal<SetupState | null>(null);

  readonly hasData = computed(() => (this.data()?.checkins?.length ?? 0) > 0);

  readonly setupReady = computed(() => (this.setup()?.state ?? 'ready') === 'ready');

  readonly viewState = computed<ViewState>(() =>
    resolveViewState({
      loading: this.loading(),
      error: this.error(),
      hasData: this.hasData(),
      attention: needsAttention(this.setup()),
    }),
  );

  readonly entries = new PagedList<any>(
    () => this.data()?.checkins ?? [],
    (item) => `${item.number} ${item.date} ${item.message}`,
    6,
  );

  ngOnInit() {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.error.set(null);
    this.http.get('/api/checkins').subscribe({
      next: (data: any) => {
        this.data.set(data);
        this.setup.set(data?.setup ?? null);
        this.loading.set(false);
      },
      error: () => {
        this.error.set('Failed to load checkins data');
        this.loading.set(false);
      },
    });
  }

  safeHtml(html: string): SafeHtml {
    if (!html) return '';
    return this.sanitizer.bypassSecurityTrustHtml(html);
  }
}
