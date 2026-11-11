import { Component, HostListener, inject, signal, computed, OnInit } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { PagedList } from '../../shared/paged-list';
import { ListControls } from '../../shared/list-controls';
import { ProvenanceBadge } from '../../shared/provenance-badge';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { LocalDatePipe } from '../../shared/local-format.pipes';
import { SetupNotice } from '../../shared/setup-notice';
import {
  SetupState,
  ViewState,
  needsAttention,
  resolveViewState,
} from '../../shared/setup-state';
import { OnboardingState } from '../../services/onboarding.service';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [CommonModule, ListControls, LocalDatePipe, ProvenanceBadge, SetupNotice],
  templateUrl: './dashboard.html',
  styleUrl: './dashboard.css',
})
export class Dashboard implements OnInit {
  private http = inject(HttpClient);
  private sanitizer = inject(DomSanitizer);

  data = signal<any>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);

  setup = signal<SetupState | null>(null);
  coachSetup = signal<SetupState | null>(null);
  onboarding = signal<OnboardingState | null>(null);

  readonly viewState = computed<ViewState>(() =>
    resolveViewState({
      loading: this.loading(),
      error: this.error(),
      hasData: (this.data()?.rows?.length ?? 0) > 0,
      attention: needsAttention(this.setup()),
    }),
  );

  readonly coachNeedsSetup = computed(() => (this.coachSetup()?.state ?? 'ready') !== 'ready');

  reviewTrend = signal('all');
  readonly reviews = new PagedList<any>(
    () =>
      (this.data()?.review_lifts ?? []).filter(
        (lift: any) => this.reviewTrend() === 'all' || lift.trend === this.reviewTrend(),
      ),
    (lift) => `${lift.name} ${lift.trend}`,
    6,
  );

  filterReviews(trend: string): void {
    this.reviewTrend.set(trend);
    this.reviews.reset();
  }

  ngOnInit() {
    this.refresh();
  }

  @HostListener('window:focus')
  onWindowFocus() {
    if (!this.loading()) this.refresh();
  }

  @HostListener('document:visibilitychange')
  onVisibilityChange() {
    if (document.visibilityState === 'visible' && !this.loading()) this.refresh();
  }

  refresh() {
    if (this.loading() && this.data()) return;
    this.loading.set(true);
    this.error.set(null);
    this.http.get('/api/dashboard').subscribe({
      next: (data: any) => {
        this.data.set(data);
        this.setup.set(data?.setup ?? null);
        this.coachSetup.set(data?.coach_setup ?? null);
        this.onboarding.set(data?.onboarding ?? null);
        this.loading.set(false);
      },
      error: () => {
        this.error.set('Failed to load dashboard data');
        this.loading.set(false);
      },
    });
  }

  safeHtml(html: string): SafeHtml {
    return this.sanitizer.bypassSecurityTrustHtml(html || '');
  }
}
