import { Component, computed, input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { SetupAction, SetupState, isLive, setupAction } from './setup-state';

/**
 * Explains why a surface has no data, or why the data it shows needs care, and
 * offers the next verified setup step.
 *
 * The notice never invents content: it renders the state, the reason and the
 * action exactly as the backend derived them for this tenant.  A running
 * connect or sync is announced politely as a status; a failure is announced as
 * an alert.
 */
@Component({
  selector: 'app-setup-notice',
  standalone: true,
  imports: [CommonModule],
  template: `
    <section
      class="setup-notice"
      [attr.data-state]="state()"
      [attr.role]="role()"
      [attr.aria-busy]="live() ? 'true' : null"
    >
      <p class="eyebrow">{{ eyebrow() }}</p>
      <h2>{{ heading() }}</h2>
      @if (reason(); as text) {
        <p>{{ text }}</p>
      }
      @if (action(); as next) {
        <p class="setup-next">
          <span>Next step</span>
          <a class="btn" [href]="next.href">{{ next.label }}</a>
        </p>
        <p class="muted">{{ next.description }}</p>
      }
      @if (lastActivity(); as activity) {
        <p class="muted">Last recorded activity: {{ activity | date: 'medium' }}</p>
      }
    </section>
  `,
})
export class SetupNotice {
  readonly setup = input<SetupState | null>(null);
  readonly headingOverride = input<string | null>(null);
  readonly reasonOverride = input<string | null>(null);

  readonly state = computed(() => this.setup()?.state ?? 'unknown');

  readonly live = computed(() => isLive(this.setup()));

  readonly role = computed(() => {
    switch (this.state()) {
      case 'degraded':
        return 'alert';
      case 'connecting':
      case 'syncing':
      case 'stale':
        return 'status';
      default:
        return null;
    }
  });

  readonly eyebrow = computed(() => {
    switch (this.state()) {
      case 'setup_required':
        return this.setup()?.first_run ? 'Welcome — setup required' : 'Setup incomplete';
      case 'connecting':
        return 'Connecting';
      case 'syncing':
        return 'Sync in progress';
      case 'stale':
        return 'Data out of date';
      case 'degraded':
        return 'Needs attention';
      default:
        return 'Setup';
    }
  });

  readonly heading = computed(
    () => this.headingOverride() ?? this.setup()?.label ?? 'Finish setting up your training data',
  );

  readonly reason = computed(() => this.reasonOverride() ?? this.setup()?.reason ?? '');

  readonly action = computed<SetupAction | null>(() => setupAction(this.setup()));

  readonly lastActivity = computed(() => this.setup()?.last_activity_at ?? null);
}
