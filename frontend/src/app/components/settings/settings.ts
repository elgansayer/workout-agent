import { Component, computed, inject, signal, OnDestroy, OnInit } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

import { environment } from '../../../environments/environment';

export interface AccountExport {
  id: string;
  status: 'queued' | 'running' | 'ready' | 'failed' | 'expired';
  requested_at: string;
  completed_at: string | null;
  expires_at: string | null;
  size_bytes: number | null;
  checksum_sha256: string | null;
  error_code: string | null;
  download_url: string | null;
}

interface AccountExportList {
  exports: AccountExport[];
  retention_hours: number;
  enabled: boolean;
}

const EXPORT_POLL_MS = 2000;

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

  exports = signal<AccountExport[]>([]);
  exportsLoaded = signal(false);
  exportsEnabled = signal(true);
  exportRetentionHours = signal(24);
  exportBusy = signal(false);
  exportStatus = signal('');
  exportInFlight = computed(() =>
    this.exports().some((item) => item.status === 'queued' || item.status === 'running'),
  );
  private exportPoll: ReturnType<typeof setTimeout> | null = null;
  private destroyed = false;

  ngOnInit() {
    this.loadSettings();
  }

  ngOnDestroy() {
    this.destroyed = true;
    this.stopExportPolling();
  }

  selectSection(key: string) {
    this.section.set(key);
    if (key === 'data') {
      this.loadExports();
    } else {
      this.stopExportPolling();
    }
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

  loadExports(): void {
    this.stopExportPolling();
    this.http.get<AccountExportList>('/api/account/exports').subscribe({
      next: (res) => {
        if (this.destroyed) return;
        this.exports.set(res.exports ?? []);
        this.exportRetentionHours.set(res.retention_hours ?? 24);
        this.exportsEnabled.set(res.enabled !== false);
        this.exportsLoaded.set(true);
        if (this.exportInFlight() && this.section() === 'data') {
          this.exportPoll = setTimeout(() => this.loadExports(), EXPORT_POLL_MS);
        }
      },
      error: (err: HttpErrorResponse) => {
        if (this.destroyed) return;
        this.exportsLoaded.set(true);
        this.exportStatus.set(
          err.status === 401 ? 'Sign in again to manage exports.' : 'Could not load your exports.',
        );
      },
    });
  }

  requestExport(): void {
    if (this.exportBusy() || this.exportInFlight() || !this.exportsEnabled()) return;
    this.exportBusy.set(true);
    this.exportStatus.set('Requesting your export…');
    this.http.post('/api/account/exports', {}).subscribe({
      next: () => {
        this.exportBusy.set(false);
        this.exportStatus.set('Export requested. It will be ready shortly.');
        this.loadExports();
      },
      error: (err: HttpErrorResponse) => {
        this.exportBusy.set(false);
        this.exportStatus.set(this.requestErrorMessage(err));
      },
    });
  }

  deleteExport(item: AccountExport): void {
    const building = item.status === 'queued' || item.status === 'running';
    if (!confirm(building ? 'Cancel and delete this export?' : 'Delete this export now?')) return;
    this.http.delete(`/api/account/exports/${encodeURIComponent(item.id)}`).subscribe({
      next: () => {
        this.exportStatus.set('Export deleted.');
        this.loadExports();
      },
      error: (err: HttpErrorResponse) => {
        this.exportStatus.set(err.error?.detail || 'Could not delete the export.');
      },
    });
  }

  downloadHref(item: AccountExport): string | null {
    if (item.status !== 'ready' || !item.download_url) return null;
    const base = environment.apiUrl ? environment.apiUrl.replace(/\/$/, '') : '';
    return `${base}${item.download_url}`;
  }

  exportStatusLabel(status: AccountExport['status']): string {
    return {
      queued: 'Queued',
      running: 'Building',
      ready: 'Ready',
      failed: 'Failed',
      expired: 'Expired',
    }[status];
  }

  exportFailureMessage(code: string | null): string {
    switch (code) {
      case 'export_too_large':
        return 'The export was too large to build. Contact the server operator.';
      case 'export_timeout':
        return 'The export took too long to build. Try again later.';
      case 'interrupted':
        return 'The export was interrupted. Request a new one.';
      case 'export_disabled':
        return 'Exports are currently disabled by the server operator.';
      default:
        return 'The export could not be built. Request a new one.';
    }
  }

  formatBytes(bytes: number | null): string {
    if (bytes === null) return '';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  private requestErrorMessage(err: HttpErrorResponse): string {
    switch (err.status) {
      case 401:
        return 'Sign in again to request an export.';
      case 429:
        return 'You have reached the export limit for today. Try again later.';
      case 503:
        return 'Data exports are temporarily unavailable.';
      default:
        return err.error?.detail || 'Could not request an export.';
    }
  }

  private stopExportPolling(): void {
    if (this.exportPoll !== null) {
      clearTimeout(this.exportPoll);
      this.exportPoll = null;
    }
  }
}
