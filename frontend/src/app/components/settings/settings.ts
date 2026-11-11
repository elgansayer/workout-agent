import { Component, computed, inject, signal, OnInit } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute } from '@angular/router';
import { IntegrationStatus } from '../../shared/integration-status';
import { StateBadge, StatusNotice } from '../../shared/status-widgets';

interface SettingsPayload {
  user_prefs?: {
    preferred_ai?: string;
    ai_model?: string;
    goals?: string[];
    experience_level?: string;
    constraints?: string[];
  };
  ai_providers?: { id: string }[];
  user_keys?: Record<string, { model?: string } | undefined>;
}

const SECTIONS = ['connections', 'ai', 'profile', 'notifications'];

interface HevyVerifyResponse {
  status: string;
  detail?: string;
  workout_count?: number;
}

interface HevySyncResponse {
  status: string;
  processed: number;
  workouts_found: number;
}

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [CommonModule, FormsModule, StateBadge, StatusNotice],
  templateUrl: './settings.html',
  styleUrl: './settings.css',
})
export class Settings implements OnInit {
  private http = inject(HttpClient);
  private route = inject(ActivatedRoute, { optional: true });

  data = signal<any>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);

  /** Connector readiness from the shared state model (`integrations` in /api/settings). */
  private component(name: string) {
    return computed<IntegrationStatus | null>(
      () => this.data()?.integrations?.components?.[name] ?? null,
    );
  }
  readonly hevy = this.component('hevy');
  readonly aiCoach = this.component('ai_provider');
  readonly googleHealth = this.component('google_health');

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

  ngOnInit() {
    // Banners elsewhere deep-link here, e.g. /settings?section=ai.
    const requested = this.route?.snapshot.queryParamMap.get('section') ?? '';
    if (SECTIONS.includes(requested)) this.section.set(requested);
    this.loadSettings();
  }

  loadSettings(populateForm = true) {
    this.error.set(null);
    this.http.get('/api/settings').subscribe({
      next: (res: SettingsPayload) => {
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
        error: (err: { error?: { detail?: string } }) => {
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
      error: (err: HttpErrorResponse) => {
        alert(err.error?.detail || 'Could not delete key.');
      },
    });
  }

  verifyHevy() {
    this.hevyStatus.set('Verifying...');
    // Without a replacement key the server verifies the stored credential and
    // records the result, which is what moves Hevy out of "connecting".
    const typed = this.hevyKeyInput.trim();
    this.http.post<HevyVerifyResponse>('/api/settings/verify-hevy', typed ? { api_key: typed } : {}).subscribe({
      next: (res: HevyVerifyResponse) => {
        if (res.status === 'ok') {
          this.hevyStatus.set(`✓ Connected! ${res.workout_count} workouts found.`);
        } else {
          this.hevyStatus.set(res.detail || 'Verification failed.');
        }
        this.loadSettings(false);
      },
      error: (err: HttpErrorResponse) => {
        this.hevyStatus.set(err.error?.detail || 'Verification failed.');
        this.loadSettings(false);
      },
    });
  }

  syncHistory() {
    if (!confirm('This will rebuild your workout history from Hevy. Continue?')) return;
    this.hevyStatus.set('Syncing history from Hevy...');
    this.http.post<HevySyncResponse>('/api/settings/sync-history', {}).subscribe({
      next: (res: HevySyncResponse) => {
        this.hevyStatus.set(
          res.status === 'partial'
            ? `Partly synced: ${res.processed} of ${res.workouts_found} workouts saved. Run the sync again.`
            : `✓ Synced! ${res.processed} of ${res.workouts_found} workouts processed.`,
        );
        this.loadSettings(false);
      },
      error: (err: HttpErrorResponse) => {
        this.hevyStatus.set(err.error?.detail || 'Sync failed.');
        this.loadSettings(false);
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
        // The selected provider decides which AI credential the badge describes.
        this.loadSettings(false);
      },
      error: (err: HttpErrorResponse) => {
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
}
