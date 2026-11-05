import { Component, inject, signal, OnInit } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import {
  HevyConnection,
  HevyVerificationResult,
  SaveKeyResult,
  SettingsResponse,
  SyncHistoryResult,
} from './settings.types';

/**
 * Read the human readable reason out of a failed request.
 *
 * The API answers with `{"detail": "..."}`. Anything else (a proxy error page,
 * a dropped connection, a non-JSON body) must not be shown as if it were a
 * product message.
 */
function errorDetail(err: unknown): string | null {
  if (!(err instanceof HttpErrorResponse)) {
    return null;
  }
  const body: unknown = err.error;
  if (body !== null && typeof body === 'object') {
    const detail: unknown = (body as { detail?: unknown }).detail;
    if (typeof detail === 'string') {
      return detail.trim() || null;
    }
  }
  return null;
}

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './settings.html',
  styleUrl: './settings.css',
})
export class Settings implements OnInit {
  private http = inject(HttpClient);

  data = signal<SettingsResponse | null>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);

  section = signal('connections');
  pushStatus = signal('');
  healthStatus = signal('');
  disconnectingHealth = signal(false);
  pushBusy = signal(false);
  hevyKeyInput = '';
  hevyStatus = signal<string>('');
  hevyConnection = signal<HevyConnection | null>(null);
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

  ngOnInit() {
    this.loadSettings();
  }

  loadSettings(populateForm = true) {
    this.error.set(null);
    this.http.get<SettingsResponse>('/api/settings').subscribe({
      next: (res) => {
        this.data.set(res);
        this.hevyConnection.set(res.hevy_connection ?? null);
        if (populateForm) {
          const prefs = res.user_prefs;
          const selectedProvider = prefs?.preferred_ai || 'gemini';
          this.selectedAiProvider.set(selectedProvider);
          for (const provider of res.ai_providers ?? []) {
            this.aiModelInputs[provider.id] = res.user_keys?.[provider.id]?.model ?? '';
          }
          if (prefs?.ai_model) {
            this.aiModelInputs[selectedProvider] = prefs.ai_model;
          }
          this.selectedGoals.set([...(prefs?.goals ?? [])]);
          this.selectedExperience.set(prefs?.experience_level || 'intermediate');
          this.constraintsInput = (prefs?.constraints ?? []).join('. ');
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

    this.http.post<SaveKeyResult>('/api/settings/key', { provider, api_key: key, model: model || undefined }).subscribe({
      next: (res) => {
        if (provider === 'hevy') {
          const connection = res.connection;
          this.hevyStatus.set(
            connection ? this.describeHevyConnection(connection) : 'Hevy key saved.',
          );
          this.hevyKeyInput = '';
        } else {
          this.aiStatus.set(`${provider} key saved!`);
          this.aiKeyInputs[provider] = '';
        }
        this.loadSettings(false);
      },
      error: (err: unknown) => {
        const msg = errorDetail(err) ?? 'Error saving key.';
        if (provider === 'hevy') this.hevyStatus.set(msg);
        else this.aiStatus.set(msg);
      },
    });
  }

  deleteKey(provider: string) {
    if (!confirm(`Remove your ${provider} API key?`)) return;
    this.http.post<{ status: string; provider: string }>('/api/settings/key/delete', { provider }).subscribe({
      next: () => {
        this.loadSettings(false);
      },
      error: (err: unknown) => {
        alert(errorDetail(err) ?? 'Could not delete key.');
      },
    });
  }

  /**
   * Test a Hevy credential without activating it.
   *
   * With a key typed into the form the candidate is tested and the form keeps
   * the key so it can be saved without retyping. Without one the stored key
   * of the authenticated user is tested and the stored connection metadata is
   * refreshed.
   */
  verifyHevy() {
    const candidate = this.hevyKeyInput.trim();
    this.hevyStatus.set('Verifying the Hevy API key...');
    this.http.post<HevyVerificationResult>('/api/settings/verify-hevy', candidate ? { api_key: candidate } : {}).subscribe({
      next: (res) => {
        this.hevyStatus.set(this.describeHevyConnection(res));
        if (!candidate) {
          // Only the stored credential can change the stored connection.
          this.loadSettings(false);
        }
      },
      error: (err: unknown) => {
        this.hevyStatus.set(errorDetail(err) ?? 'Could not verify the Hevy API key.');
      },
    });
  }

  describeHevyConnection(res: HevyConnection): string {
    if (res.state !== 'connected') {
      // Never claim a connection the API did not confirm.
      return `✗ Not connected (${res.state.replace(/_/g, ' ')}).`;
    }
    const account = res.account_username ? ` as ${res.account_username}` : '';
    const version = res.api_version ? ` · API ${res.api_version}` : '';
    const capabilities = Object.entries(res.capabilities ?? {})
      .filter(([, enabled]) => enabled === true)
      .map(([name]) => name.replace(/_/g, ' '));
    const capabilityText = capabilities.length ? ` · ${capabilities.join(', ')}` : '';
    return `✓ Connected${account}${version}${capabilityText}.`;
  }

  hevyConnectionLabel(): string {
    const state = this.hevyConnection()?.state;
    if (state === 'connected') return 'Connected';
    if (state === 'invalid' || state === 'insufficient_scope') return 'Needs attention';
    return 'Not verified';
  }

  hevyConnectionDetail(): string {
    const state = this.hevyConnection()?.state;
    if (state === 'invalid') {
      return 'Hevy rejected the stored API key. Use Verify to test a new key.';
    }
    if (state === 'insufficient_scope') {
      return 'The stored API key cannot read your Hevy workouts. Use Verify to test a new key.';
    }
    if (state === 'unverified') {
      return 'The stored API key could not be verified. Use Verify to try again.';
    }
    return 'The stored Hevy key has not been verified yet. Use Verify to test it.';
  }

  syncHistory() {
    if (!confirm('This will rebuild your workout history from Hevy. Continue?')) return;
    this.hevyStatus.set('Syncing workout history from Hevy...');
    this.http.post<SyncHistoryResult>('/api/settings/sync-history', {}).subscribe({
      next: (res) => {
        this.hevyStatus.set(
          `✓ Synced! ${res.processed} of ${res.workouts_found} workouts processed.`,
        );
      },
      error: (err: unknown) => {
        this.hevyStatus.set(errorDetail(err) ?? 'Sync failed.');
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

    this.http.post<{ status: string }>('/api/settings/preferences', payload).subscribe({
      next: () => {
        this.prefStatus.set('Profile saved!');
        setTimeout(() => this.prefStatus.set(''), 4000);
      },
      error: (err: unknown) => {
        this.prefStatus.set(errorDetail(err) ?? 'Could not save preferences.');
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
      this.pushStatus.set('Push notifications disabled.');
    } catch {
      this.pushBusy.set(false);
      this.pushStatus.set(
        'Could not update notifications. Check browser permissions and try again.',
      );
    }
  }
}
