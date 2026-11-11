import { Component, computed, input } from '@angular/core';
import { IntegrationStatus, needsAttention, stateLabel } from './integration-status';

/** A state chip. The label is text, so state is never conveyed by colour alone. */
@Component({
  selector: 'app-state-badge',
  standalone: true,
  template: `
    @if (label(); as text) {
      <span class="status state-badge" [attr.data-state]="state()">{{ text }}</span>
    }
  `,
})
export class StateBadge {
  readonly state = input<string | null | undefined>();
  protected readonly label = computed(() => stateLabel(this.state()));
}

/**
 * Renders a connector's shared status message and next action. Hidden while the
 * connector is `ready` unless `showReady` is set. `inline` drops the panel
 * chrome so it can sit inside an existing card; `showAction` controls the link.
 */
@Component({
  selector: 'app-status-notice',
  standalone: true,
  imports: [StateBadge],
  template: `
    @if (visible(); as s) {
      <div
        [class]="inline() ? 'status-notice status-inline' : 'panel status-notice'"
        [attr.data-state]="s.state"
        [attr.data-connector]="s.connector"
        [attr.role]="needsAttention(s.state) ? 'alert' : 'status'"
      >
        @if (!inline()) {
          <div class="status-notice-head">
            <strong>{{ s.label }}</strong>
            <app-state-badge [state]="s.state" />
          </div>
        }
        <p class="status-notice-message">{{ s.message }}</p>
        @if (showAction() && s.action_href && s.action_label) {
          <a class="btn btn-ghost" [href]="s.action_href">{{ s.action_label }}</a>
        }
      </div>
    }
  `,
})
export class StatusNotice {
  readonly status = input<IntegrationStatus | null | undefined>();
  readonly showReady = input(false);
  readonly inline = input(false);
  /** Pages that already host the action (Settings) hide the self-link. */
  readonly showAction = input(true);
  protected readonly needsAttention = needsAttention;
  protected readonly visible = computed(() => {
    const status = this.status();
    if (!status) return null;
    return status.state === 'ready' && !this.showReady() ? null : status;
  });
}
