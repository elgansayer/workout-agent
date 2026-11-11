import { HlmCardImports } from '@spartan-ng/ui/card';
import { HlmBadgeImports } from '@spartan-ng/ui/badge';
import { HlmAlertImports } from '@spartan-ng/ui/alert';
import { Component, inject, signal, computed, OnInit } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { FormsModule } from '@angular/forms';
import { LocalDatePipe } from '../../shared/local-format.pipes';
import { ProvenanceBadge } from '../../shared/provenance-badge';
import { SetupNotice } from '../../shared/setup-notice';
import { SetupState, ViewState, resolveViewState } from '../../shared/setup-state';

@Component({
  selector: 'app-plan',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    HlmCardImports,
    HlmBadgeImports,
    HlmAlertImports,
    LocalDatePipe,
    ProvenanceBadge,
    SetupNotice,
  ],
  templateUrl: './plan.html',
  styleUrl: './plan.css',
})
export class Plan implements OnInit {
  private http = inject(HttpClient);
  protected sanitizer = inject(DomSanitizer);

  data = signal<any>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);
  setup = signal<SetupState | null>(null);

  readonly hasData = computed(() => !!this.data() && !this.data()?.setup_required);

  readonly viewState = computed<ViewState>(() =>
    resolveViewState({
      loading: this.loading(),
      error: this.error(),
      hasData: this.hasData(),
      stale: this.setup()?.stale ?? false,
    }),
  );

  ngOnInit() {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.error.set(null);
    this.http.get('/api/plan').subscribe({
      next: (data: any) => {
        this.data.set(data);
        this.setup.set(data?.setup ?? null);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set('Failed to load plan data');
        this.loading.set(false);
      },
    });
  }

  safeHtml(html: string): SafeHtml {
    if (!html) return '';
    return this.sanitizer.bypassSecurityTrustHtml(html);
  }
}
