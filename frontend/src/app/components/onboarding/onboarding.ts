import { Component, OnInit, inject, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { Observable } from 'rxjs';
import {
  OnboardingService,
  OnboardingState,
  OnboardingStep,
} from '../../services/onboarding.service';

/** Resumable first-run setup derived from verified server state. */
@Component({
  selector: 'app-onboarding',
  standalone: true,
  templateUrl: './onboarding.html',
  styleUrl: './onboarding.css',
})
export class Onboarding implements OnInit {
  protected readonly onboarding = inject(OnboardingService);
  protected readonly busy = signal(false);
  protected readonly status = signal('');
  protected readonly error = signal<string | null>(null);

  ngOnInit(): void {
    this.onboarding.load(true);
  }

  protected skip(step: OnboardingStep): void {
    this.mutate(
      () => this.onboarding.skip(step.id),
      `Skipped ${step.title}. ${step.skip_consequence ?? ''}`.trim(),
    );
  }

  protected resume(step: OnboardingStep): void {
    this.mutate(() => this.onboarding.resume(step.id), `${step.title} is back in your setup.`);
  }

  private mutate(request: () => Observable<OnboardingState>, message: string): void {
    if (this.busy()) return;
    this.busy.set(true);
    this.error.set(null);
    request().subscribe({
      next: () => {
        this.status.set(message);
        this.busy.set(false);
      },
      error: (err: unknown) => {
        this.status.set('');
        this.error.set(
          err instanceof HttpErrorResponse && typeof err.error?.detail === 'string'
            ? err.error.detail
            : 'Could not update setup. Try again.',
        );
        this.busy.set(false);
      },
    });
  }
}
