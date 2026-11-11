import { Component, inject, signal, OnInit } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpErrorResponse } from '@angular/common/http';
import { DisplaySettings, DisplaySettingsService } from '../../services/display-settings';

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './settings.html',
  styleUrl: './settings.css',
})
export class Settings implements OnInit {
  private http = inject(HttpClient);
  protected readonly display = inject(DisplaySettingsService);

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

  displayDraft = signal<DisplaySettings>({ ...this.display.settings() });
  displayStatus = signal<string>('');
  displayStatusIsError = signal<boolean>(false);
  displaySaving = signal<boolean>(false);

  ngOnInit() {
    this.loadSettings();
    void this.loadDisplay();
  }

  async loadDisplay(): Promise<void> {
    await this.display.load();
    this.displayDraft.set({ ...this.display.settings() });
  }

  setDisplayField<K extends keyof DisplaySettings>(field: K, value: DisplaySettings[K]): void {
    this.displayDraft.set({ ...this.displayDraft(), [field]: value });
  }

  /** Offer the browser's zone, but only when this server supports it. */
  useDeviceTimeZone(): void {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (zone && this.display.options()?.timezones.includes(zone)) {
      this.setDisplayField('timezone', zone);
      this.reportDisplay(`Time zone set to ${zone}. Save to apply it.`, false);
    } else {
      this.reportDisplay(
        `This device reports ${zone || 'no time zone'}, which Workout Agent does not support. Pick one from the list.`,
        true,
      );
    }
  }

  async saveDisplay(): Promise<void> {
    if (this.displaySaving()) return;
    const current = this.display.settings();
    const draft = this.displayDraft();
    const changes: Partial<DisplaySettings> = {};
    for (const key of Object.keys(draft) as (keyof DisplaySettings)[]) {
      if (draft[key] !== current[key]) {
        (changes as Record<string, string>)[key] = draft[key];
      }
    }
    if (Object.keys(changes).length === 0) {
      this.reportDisplay('No changes to save.', false);
      return;
    }

    this.displaySaving.set(true);
    try {
      const saved = await this.display.save(changes);
      this.displayDraft.set({ ...saved.settings });
      const regrouped =
        saved.regrouped_days > 0
          ? ` ${saved.regrouped_days} workout ${saved.regrouped_days === 1 ? 'day was' : 'days were'} regrouped for the new time zone; your recorded times are unchanged.`
          : '';
      this.reportDisplay(`Saved.${regrouped}`, false);
    } catch (err) {
      const detail = err instanceof HttpErrorResponse ? err.error?.detail : null;
      this.reportDisplay(
        typeof detail === 'string' ? detail : 'Could not save your settings. Try again.',
        true,
      );
    } finally {
      this.displaySaving.set(false);
    }
  }

  private reportDisplay(message: string, isError: boolean): void {
    this.displayStatus.set(message);
    this.displayStatusIsError.set(isError);
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
}
