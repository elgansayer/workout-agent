import { Component, computed, input } from '@angular/core';

export type ReadinessPhase =
  'setup_required' | 'connecting' | 'syncing' | 'ready' | 'stale' | 'degraded';

export interface ProductReadiness {
  phase: ReadinessPhase;
  message: string;
  next_action: string;
  setup_steps: Array<{ key: string; label: string; complete: boolean }>;
  data: { available: boolean };
  provider: { available: boolean };
}

@Component({
  selector: 'app-readiness-banner',
  standalone: true,
  template: `
    @if (readiness(); as readiness) {
      @if (readiness.phase && readiness.phase !== 'ready') {
        <section
          class="panel readiness-banner"
          [class.error-panel]="readiness.phase === 'degraded'"
          [attr.role]="readiness.phase === 'degraded' ? 'alert' : 'status'"
        >
          <h2>{{ title() }}</h2>
          <p class="muted">{{ readiness.message }}</p>
          @if (showSteps()) {
            <ul class="readiness-steps">
              @for (step of readiness.setup_steps; track step.key) {
                <li [class.done]="step.complete">
                  <span aria-hidden="true">{{ step.complete ? '✓' : '○' }}</span>
                  {{ step.label }}
                </li>
              }
            </ul>
          }
          @if (readiness.next_action && readiness.next_action !== '/dashboard') {
            <a class="btn" [href]="readiness.next_action">{{ actionLabel() }}</a>
          }
        </section>
      }
    }
  `,
})
export class ReadinessBanner {
  readonly readiness = input<ProductReadiness | null>(null);
  readonly context = input<string>('setup');

  readonly title = computed(() => {
    switch (this.readiness()?.phase) {
      case 'setup_required':
        return 'Finish setting up';
      case 'connecting':
        return 'Connecting your data';
      case 'syncing':
        return 'Syncing your data';
      case 'stale':
        return 'Your data is out of date';
      case 'degraded':
        return 'Something needs attention';
      default:
        return 'Status';
    }
  });

  readonly actionLabel = computed(() => {
    const provider = this.readiness()?.provider;
    if (provider && !provider.available) return 'Open AI settings';
    return 'Open settings';
  });

  readonly showSteps = computed(() => {
    const readiness = this.readiness();
    if (!readiness) return false;
    return !readiness.data?.available || !readiness.provider?.available;
  });
}
