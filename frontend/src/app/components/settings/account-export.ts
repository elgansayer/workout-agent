import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { CommonModule } from '@angular/common';

export type ExportState = 'queued' | 'running' | 'ready' | 'failed' | 'expired';

/** One export request, exactly as `/api/account/exports` reports it. */
export interface ExportRecord {
  export_id: string;
  state: ExportState;
  requested_at: string;
  started_at: string | null;
  completed_at: string | null;
  expires_at: string | null;
  size_bytes: number | null;
  sha256: string | null;
  record_count: number | null;
  format_version: number;
  attempts: number;
  error_code: string | null;
  can_download: boolean;
  /** Present only while the archive can actually be downloaded. */
  download_url: string | null;
}

export interface ExportInfo {
  eligible: boolean;
  reason: string | null;
  retention_hours: number;
  daily_limit: number;
  remaining_today: number;
  format_version: number;
  included: { category: string; label: string }[];
  excluded: { category: string; reason: string }[];
  exports: ExportRecord[];
}

const FAILURES: Record<string, string> = {
  too_large:
    'Your data is larger than this server allows in one export. Ask the administrator to raise the export size limit.',
  not_eligible: 'This account could not be exported. It may be suspended or being erased.',
  timed_out: 'The export took too long and was stopped. Request it again.',
};
const GENERIC_FAILURE = 'The export could not be built. Request it again in a few minutes.';

const REQUEST_ERRORS: Record<string, string> = {
  rate_limited: 'You have reached the daily export limit. Try again tomorrow.',
  not_eligible: 'Exports are only available when you are signed in with your own account.',
};

/** How often the list is re-read while an export is open or waiting to expire. */
const POLL_MS: Partial<Record<ExportState, number>> = { queued: 3000, running: 3000, ready: 60000 };

/**
 * Lets the signed-in user ask for, follow and download a complete copy of their
 * data. The archive is built by the background worker, so the panel follows the
 * request until it is ready; the download link is only shown while the server
 * says the archive can still be fetched.
 */
@Component({
  selector: 'app-account-export',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './account-export.html',
  styleUrl: './account-export.css',
})
export class AccountExport implements OnInit, OnDestroy {
  private readonly http = inject(HttpClient);

  readonly info = signal<ExportInfo | null>(null);
  readonly busy = signal(false);
  readonly status = signal('');
  readonly error = signal('');

  readonly latest = computed<ExportRecord | null>(() => this.info()?.exports[0] ?? null);
  readonly inFlight = computed(() => {
    const state = this.latest()?.state;
    return state === 'queued' || state === 'running';
  });
  readonly canRequest = computed(
    () =>
      !!this.info()?.eligible &&
      !this.busy() &&
      !this.inFlight() &&
      (this.info()?.remaining_today ?? 0) > 0,
  );
  readonly retention = computed(() => {
    const hours = this.info()?.retention_hours ?? 0;
    return hours > 0 && hours % 24 === 0
      ? `${hours / 24} ${hours === 24 ? 'day' : 'days'}`
      : `${hours} ${hours === 1 ? 'hour' : 'hours'}`;
  });

  private poll?: ReturnType<typeof setTimeout>;

  ngOnInit(): void {
    this.load();
  }

  ngOnDestroy(): void {
    this.stopPolling();
  }

  load(): void {
    this.http.get<ExportInfo>('/api/account/exports').subscribe({
      next: (info) => this.apply(info),
      error: (err: HttpErrorResponse) => this.fail(err),
    });
  }

  request(): void {
    if (!this.canRequest()) return;
    this.busy.set(true);
    this.error.set('');
    this.status.set('');
    this.http.post<ExportRecord>('/api/account/exports', {}).subscribe({
      next: () => {
        this.busy.set(false);
        this.load();
      },
      error: (err: HttpErrorResponse) => {
        this.busy.set(false);
        this.fail(err);
      },
    });
  }

  /** Human text for an export that did not finish. */
  failureText(record: ExportRecord): string {
    return (record.error_code && FAILURES[record.error_code]) || GENERIC_FAILURE;
  }

  /** A byte count a person can read: `3.4 MB`, `812 KB`, `0 B`. */
  readableSize(bytes: number | null): string {
    if (bytes === null) return '';
    const units = ['B', 'KB', 'MB', 'GB'];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${unit === 0 || value >= 100 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
  }

  private apply(info: ExportInfo): void {
    const before = this.latest();
    this.info.set(info);
    const after = this.latest();
    if (after && (!before || before.export_id !== after.export_id || before.state !== after.state)) {
      this.status.set(this.announce(after));
    }
    this.stopPolling();
    const delay = after ? POLL_MS[after.state] : undefined;
    if (delay) this.poll = setTimeout(() => this.load(), delay);
  }

  private announce(record: ExportRecord): string {
    switch (record.state) {
      case 'queued':
        return 'Your export is queued.';
      case 'running':
        return 'Preparing your export…';
      case 'ready':
        return 'Your export is ready to download.';
      case 'expired':
        return 'Your last export has expired.';
      default:
        return this.failureText(record);
    }
  }

  private fail(err: HttpErrorResponse): void {
    const detail: unknown = err?.error?.detail;
    if (err?.status === 423) {
      this.error.set('Your data is being erased right now.');
    } else if (err?.status === 429 && detail !== 'rate_limited') {
      this.error.set('Too many requests. Wait a minute and try again.');
    } else {
      this.error.set(
        (typeof detail === 'string' && REQUEST_ERRORS[detail]) ||
          'The export request could not be completed. Please try again.',
      );
    }
  }

  private stopPolling(): void {
    if (this.poll !== undefined) clearTimeout(this.poll);
    this.poll = undefined;
  }
}
