import { Component, HostListener, inject, signal, OnInit } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { PagedList } from '../../shared/paged-list';
import { ListControls } from '../../shared/list-controls';
import { ReadinessBanner } from '../../shared/readiness-banner';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [CommonModule, ListControls, ReadinessBanner],
  templateUrl: './dashboard.html',
  styleUrl: './dashboard.css',
})
export class Dashboard implements OnInit {
  private http = inject(HttpClient);
  private sanitizer = inject(DomSanitizer);

  data = signal<any>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);

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
