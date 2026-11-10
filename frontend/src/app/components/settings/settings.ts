import { Component, inject, signal, OnInit, OnDestroy } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

type DeletionScope = 'account' | 'health_data';

interface DeletionStep {
  step: string;
  label: string;
  state: 'pending' | 'done';
  rows_affected: number;
  remaining_after: number | null;
}

interface DeletionRequest {
  request_id: string;
  scope: DeletionScope;
  state: 'pending' | 'in_progress' | 'failed' | 'completed' | 'cancelled';
  requested_at: string;
  execute_after: string;
  completed_at: string | null;
  cancelled_at: string | null;
  can_cancel: boolean;
  attempts: number;
  error_code: string | null;
  steps: DeletionStep[];
  progress_percent: number;
}

interface DeletionInfo {
  eligible: boolean;
  reason: string | null;
  scopes?: { scope: DeletionScope; required_confirmation: string; grace_days: number }[];
  request: DeletionRequest | null;
}

const DELETION_ERRORS: Record<string, string> = {
  confirmation_mismatch: 'The confirmation text did not match exactly.',
  already_requested: 'A deletion request is already open for this account.',
  not_eligible: 'This account cannot be deleted from here.',
  not_cancellable: 'The cancellation window has closed.',
  not_found: 'There is no open deletion request to cancel.',
};

// How often an open request is re-read: running erasure shows live progress,
// a pending closure only needs to notice when its window ends.
const POLL_MS: Record<string, number> = { in_progress: 2000, failed: 15000, pending: 60000 };

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './settings.html',
  styleUrl: './settings.css',
})
export class Settings implements OnInit, OnDestroy {
  private http = inject(HttpClient);

  data = signal<any>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);

  section = signal('connections');
  pushStatus = signal('');
  healthStatus = signal('');
  disconnectingHealth = signal(false);
  pushBusy = signal(false);
  hevyKeyInput = '';
  hevyStatus = signal<string>('');
  aiStatus = signal<string>('');
  prefStatus = signal<string>('');
  pushSubscribed = signal<boolean>(false);

  selectedAiProvider = signal<string>('gemini');
  aiKeyInputs: { [key: string]: string } = {};
  aiModelInputs: { [key: string]: string } = {};

  availableGoals = ['strength', 'hypertrophy', 'endurance', 'fat_loss', 'power'];
  selectedGoals = signal<string[]>([]);
  selectedExperience = signal<string>('intermediate');
  constraintsInput = '';

  deletion = signal<DeletionInfo | null>(null);
  deletionStatus = signal('');
  deletionError = signal('');
  deletionBusy = signal(false);
  healthConfirmation = '';
  accountConfirmation = '';
  private deletionPoll?: ReturnType<typeof setTimeout>;

  ngOnInit() {
    this.loadSettings();
    // Deep link from the Account page: /settings?section=account
    if (new URLSearchParams(window.location.search).get('section') === 'account') {
      this.selectSection('account');
    }
  }

  ngOnDestroy() {
    this.stopDeletionPoll();
  }

  selectSection(name: string) {
    this.section.set(name);
    // Deletion status is only read when the section is opened.
    if (name === 'account') this.loadDeletion();
  }

  loadSettings(populateForm = true) {
    this.error.set(null);
    this.http.get('/api/settings').subscribe({
      next: (res: any) => {
        this.data.set(res);
        if (populateForm) {
          const prefs = res.user_prefs || {};
          const selectedProvider = prefs.preferred_ai || 'gemini';
          this.selectedAiProvider.set(selectedProvider);
          for (const provider of res.ai_providers || []) {
            this.aiModelInputs[provider.id] = res.user_keys?.[provider.id]?.model || '';
          }
          if (prefs.ai_model) {
            this.aiModelInputs[selectedProvider] = prefs.ai_model;
          }
          this.selectedGoals.set(prefs.goals || []);
          this.selectedExperience.set(prefs.experience_level || 'intermediate');
          this.constraintsInput = (prefs.constraints || []).join('. ');
        }
        void this.checkPushSubscription();
        this.loading.set(false);
      },
      error: () => {
        this.error.set('Failed to load settings');
        this.loading.set(false);
      },
    });
  }

  selectProvider(id: string) {
    this.selectedAiProvider.set(id);
  }

  toggleGoal(goal: string) {
    const current = this.selectedGoals();
    if (current.includes(goal)) {
      this.selectedGoals.set(current.filter((g) => g !== goal));
    } else {
      this.selectedGoals.set([...current, goal]);
    }
  }

  saveKey(provider: string) {
    const key =
      provider === 'hevy' ? this.hevyKeyInput.trim() : (this.aiKeyInputs[provider] || '').trim();
    const model = (this.aiModelInputs[provider] || '').trim();

    if (!key) {
      if (provider === 'hevy') {
        this.hevyStatus.set('Please enter an API key.');
      } else {
        this.aiStatus.set('Please enter an API key.');
      }
      return;
    }

    this.http
      .post('/api/settings/key', { provider, api_key: key, model: model || undefined })
      .subscribe({
        next: () => {
          if (provider === 'hevy') {
            this.hevyStatus.set('Hevy key saved!');
            this.hevyKeyInput = '';
          } else {
            this.aiStatus.set(`${provider} key saved!`);
            this.aiKeyInputs[provider] = '';
          }
          this.loadSettings(false);
        },
        error: (err: any) => {
          const msg = err.error?.detail || 'Error saving key.';
          if (provider === 'hevy') this.hevyStatus.set(msg);
          else this.aiStatus.set(msg);
        },
      });
  }

  deleteKey(provider: string) {
    if (!confirm(`Remove your ${provider} API key?`)) return;
    this.http.post('/api/settings/key/delete', { provider }).subscribe({
      next: () => {
        this.loadSettings(false);
      },
      error: (err: any) => {
        alert(err.error?.detail || 'Could not delete key.');
      },
    });
  }

  verifyHevy() {
    this.hevyStatus.set('Verifying...');
    this.http
      .post('/api/settings/verify-hevy', { api_key: this.hevyKeyInput.trim() || '__stored__' })
      .subscribe({
        next: (res: any) => {
          if (res.status === 'ok') {
            this.hevyStatus.set(`✓ Connected! ${res.workout_count} workouts found.`);
          } else {
            this.hevyStatus.set(res.detail || 'Verification failed.');
          }
        },
        error: (err: any) => {
          this.hevyStatus.set(err.error?.detail || 'Verification failed.');
        },
      });
  }

  syncHistory() {
    if (!confirm('This will rebuild your workout history from Hevy. Continue?')) return;
    this.hevyStatus.set('Syncing history from Hevy...');
    this.http.post('/api/settings/sync-history', {}).subscribe({
      next: (res: any) => {
        this.hevyStatus.set(
          `✓ Synced! ${res.processed} of ${res.workouts_found} workouts processed.`,
        );
      },
      error: (err: any) => {
        this.hevyStatus.set(err.error?.detail || 'Sync failed.');
      },
    });
  }

  savePreferences() {
    const constraints = this.constraintsInput
      ? this.constraintsInput
          .split(/[.\n]+/)
          .map((s) => s.trim())
          .filter(Boolean)
      : [];

    const payload = {
      goals: this.selectedGoals(),
      constraints,
      experience_level: this.selectedExperience(),
      preferred_ai: this.selectedAiProvider(),
      ai_model: (this.aiModelInputs[this.selectedAiProvider()] || '').trim() || undefined,
    };

    this.http.post('/api/settings/preferences', payload).subscribe({
      next: () => {
        this.prefStatus.set('Profile saved!');
        setTimeout(() => this.prefStatus.set(''), 4000);
      },
      error: (err: any) => {
        this.prefStatus.set(err.error?.detail || 'Could not save preferences.');
      },
    });
  }

  disconnectGoogleHealth(): void {
    if (this.disconnectingHealth()) return;
    this.disconnectingHealth.set(true);
    this.http.post('/google-health/disconnect', {}, { responseType: 'text' }).subscribe({
      next: () => {
        this.disconnectingHealth.set(false);
        this.healthStatus.set('Google Health disconnected.');
        this.loadSettings(false);
      },
      error: () => {
        this.disconnectingHealth.set(false);
        this.healthStatus.set('Could not disconnect Google Health. Please try again.');
      },
    });
  }

  async checkPushSubscription(): Promise<void> {
    if (!('serviceWorker' in navigator)) return;
    try {
      const reg = await navigator.serviceWorker.getRegistration();
      this.pushSubscribed.set(!!(await reg?.pushManager?.getSubscription()));
    } catch {
      this.pushStatus.set('Could not check notification permission.');
    }
  }

  async subscribeToPush() {
    if (this.pushBusy()) return;
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      this.pushStatus.set('Push notifications are not supported in this browser.');
      return;
    }
    this.pushBusy.set(true);
    this.pushStatus.set('');
    try {
      const reg = await navigator.serviceWorker.register('/sw.js');
      const vapidKey = this.data()?.vapid_public_key;
      if (!vapidKey) throw new Error('Notifications unavailable');
      const sub =
        (await reg.pushManager.getSubscription()) ||
        (await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: vapidKey,
        }));
      this.http.post('/api/settings/push-subscribe', sub.toJSON()).subscribe({
        next: () => {
          this.pushSubscribed.set(true);
          this.pushBusy.set(false);
          this.pushStatus.set('Notifications enabled.');
        },
        error: () => {
          this.pushBusy.set(false);
          this.pushStatus.set('Could not save notification subscription. Try again.');
        },
      });
    } catch {
      this.pushBusy.set(false);
      this.pushStatus.set(
        'Could not update notifications. Check browser permissions and try again.',
      );
    }
  }

  async unsubscribeFromPush() {
    if (this.pushBusy()) return;
    this.pushBusy.set(true);
    try {
      const reg = await navigator.serviceWorker.getRegistration();
      const sub = await reg?.pushManager.getSubscription();
      if (sub) {
        if (!(await sub.unsubscribe())) throw new Error('Unsubscribe failed');
      }
      this.pushSubscribed.set(false);
      this.pushBusy.set(false);
      this.pushStatus.set('Notifications disabled.');
    } catch {
      this.pushBusy.set(false);
      this.pushStatus.set(
        'Could not update notifications. Check browser permissions and try again.',
      );
    }
  }

  // ───────────── Account & data deletion ─────────────

  loadDeletion() {
    this.http.get<DeletionInfo>('/api/account/deletion').subscribe({
      next: (info) => {
        this.deletion.set(info);
        this.scheduleDeletionPoll(info.request);
      },
      error: (err: HttpErrorResponse) => this.handleDeletionFailure(err),
    });
  }

  requiredConfirmation(scope: DeletionScope): string {
    return (
      this.deletion()?.scopes?.find((item) => item.scope === scope)?.required_confirmation ?? ''
    );
  }

  graceDays(): number {
    return this.deletion()?.scopes?.find((item) => item.scope === 'account')?.grace_days ?? 0;
  }

  confirmationMatches(scope: DeletionScope): boolean {
    const typed = scope === 'account' ? this.accountConfirmation : this.healthConfirmation;
    const required = this.requiredConfirmation(scope);
    const normalise = (text: string) => text.trim().replace(/\s+/g, ' ').toLowerCase();
    return required !== '' && normalise(typed) === normalise(required);
  }

  isOpen(request: DeletionRequest | null | undefined): boolean {
    return !!request && ['pending', 'in_progress', 'failed'].includes(request.state);
  }

  requestDeletion(scope: DeletionScope) {
    if (this.deletionBusy() || !this.confirmationMatches(scope)) return;
    this.deletionBusy.set(true);
    this.deletionError.set('');
    const confirmation = scope === 'account' ? this.accountConfirmation : this.healthConfirmation;
    this.http.post<DeletionRequest>('/api/account/deletion', { scope, confirmation }).subscribe({
      next: (request) => {
        this.deletionBusy.set(false);
        this.healthConfirmation = '';
        this.accountConfirmation = '';
        this.deletion.update((info) => (info ? { ...info, request } : info));
        if (request.state === 'completed' && request.scope === 'account') {
          void this.finishAccountClosure();
          return;
        }
        this.deletionStatus.set(this.describeRequest(request));
        this.scheduleDeletionPoll(request);
      },
      error: (err: HttpErrorResponse) => {
        this.deletionBusy.set(false);
        this.handleDeletionFailure(err);
      },
    });
  }

  cancelDeletion() {
    if (this.deletionBusy()) return;
    this.deletionBusy.set(true);
    this.deletionError.set('');
    this.http.post<DeletionRequest>('/api/account/deletion/cancel', {}).subscribe({
      next: (request) => {
        this.deletionBusy.set(false);
        this.deletion.update((info) => (info ? { ...info, request } : info));
        this.deletionStatus.set('Account closure cancelled. Nothing was deleted.');
        this.stopDeletionPoll();
      },
      error: (err: HttpErrorResponse) => {
        this.deletionBusy.set(false);
        this.handleDeletionFailure(err);
        this.loadDeletion();
      },
    });
  }

  describeRequest(request: DeletionRequest): string {
    switch (request.state) {
      case 'pending':
        return 'Account closure is scheduled. You can cancel until the date shown.';
      case 'in_progress':
        return 'Erasing your data…';
      case 'failed':
        return 'Erasure hit a problem and is being retried automatically.';
      case 'completed':
        return request.scope === 'account'
          ? 'Your account has been closed.'
          : 'Your health data has been erased.';
      default:
        return '';
    }
  }

  private handleDeletionFailure(err: HttpErrorResponse) {
    if (err?.status === 401) {
      // A closed account's session is refused: the erasure has finished.
      void this.finishAccountClosure();
      return;
    }
    const code = err?.error?.detail;
    this.deletionError.set(
      err?.status === 423
        ? 'Your data is being erased right now.'
        : (DELETION_ERRORS[code] ?? 'The request could not be completed. Please try again.'),
    );
  }

  private scheduleDeletionPoll(request: DeletionRequest | null | undefined) {
    this.stopDeletionPoll();
    const delay = request ? POLL_MS[request.state] : undefined;
    if (delay) this.deletionPoll = setTimeout(() => this.loadDeletion(), delay);
  }

  private stopDeletionPoll() {
    if (this.deletionPoll !== undefined) clearTimeout(this.deletionPoll);
    this.deletionPoll = undefined;
  }

  private async finishAccountClosure(): Promise<void> {
    this.stopDeletionPoll();
    this.deletionStatus.set('Your account has been closed and your data erased.');
    try {
      // The server already deleted its copy; also drop this browser's push subscription.
      const reg = await navigator.serviceWorker?.getRegistration();
      await (await reg?.pushManager?.getSubscription())?.unsubscribe();
    } catch {
      // Best effort only.
    }
    this.navigate('/logout');
  }

  protected navigate(url: string) {
    window.location.assign(url);
  }
}
