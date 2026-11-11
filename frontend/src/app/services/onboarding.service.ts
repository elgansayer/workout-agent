import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, tap } from 'rxjs';

export type OnboardingStepStatus = 'complete' | 'pending' | 'skipped';

export interface OnboardingStep {
  readonly id: string;
  readonly title: string;
  readonly description: string;
  readonly href: string;
  readonly optional: boolean;
  readonly status: OnboardingStepStatus;
  readonly skip_consequence: string | null;
}

export interface OnboardingState {
  readonly steps: readonly OnboardingStep[];
  readonly next_step: OnboardingStep | null;
  readonly account_ready: boolean;
  readonly completed_count: number;
  readonly skipped_count: number;
  readonly total_steps: number;
  readonly progress_percent: number;
}

/**
 * Account-wide first-run setup state.
 *
 * The sequence and every completed step come from the server, so revisiting the
 * app, another device or a reload resumes at the same place. Only explicit
 * skips are stored, and those are also persisted against the signed-in tenant.
 */
@Injectable({ providedIn: 'root' })
export class OnboardingService {
  private readonly http = inject(HttpClient);

  readonly state = signal<OnboardingState | null>(null);
  readonly loading = signal(false);

  load(force = false): void {
    if (this.loading()) return;
    if (this.state() && !force) return;
    this.loading.set(true);
    this.http.get<OnboardingState>('/api/onboarding').subscribe({
      next: (state) => {
        this.state.set(state);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  skip(step: string): Observable<OnboardingState> {
    return this.http
      .post<OnboardingState>('/api/onboarding/skip', { step })
      .pipe(tap((state) => this.state.set(state)));
  }

  resume(step: string): Observable<OnboardingState> {
    return this.http
      .post<OnboardingState>('/api/onboarding/resume', { step })
      .pipe(tap((state) => this.state.set(state)));
  }
}
