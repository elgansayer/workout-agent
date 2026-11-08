import { Component, inject, signal, OnInit } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

export interface ProhibitedExerciseRule {
  exercise: string;
  reason?: string;
}

export interface ExerciseSubstitutionRule {
  from_exercise: string;
  to_exercise: string;
  reason?: string;
}

export interface RepIntensityBoundsRule {
  min_reps: number | null;
  max_reps: number | null;
  max_rpe: number | null;
  min_rir: number | null;
}

export interface FrequencyRule {
  max_sessions_per_week: number | null;
  min_rest_days_between_sessions: number | null;
}

export interface VolumeRule {
  max_sets_per_workout: number | null;
  max_sets_per_exercise: number | null;
}

export interface EquipmentRule {
  available_equipment: string[];
  prohibited_equipment: string[];
}

export interface RecoveryRule {
  min_sleep_hours: number | null;
  min_readiness_score: number | null;
  auto_deload_on_fatigue: boolean;
}

export interface ContextualNoteRule {
  category: string;
  note: string;
  severity: string;
}

export interface TrainingRulesData {
  version: number;
  prohibited_exercises?: ProhibitedExerciseRule[];
  substitutions?: ExerciseSubstitutionRule[];
  rep_intensity_bounds?: RepIntensityBoundsRule;
  frequency?: FrequencyRule;
  volume?: VolumeRule;
  equipment?: EquipmentRule;
  recovery?: RecoveryRule;
  contextual_notes?: ContextualNoteRule[];
  updated_at?: string | null;
}

function parseNullableNumber(val: unknown): number | null {
  if (val === null || val === undefined || val === '') {
    return null;
  }
  const num = Number(val);
  return Number.isNaN(num) ? null : num;
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

  trainingRules = signal<TrainingRulesData | null>(null);
  rulesStatus = signal<string>('');
  prohibitedExercises = signal<{ exercise: string; reason?: string }[]>([]);
  substitutions = signal<{ from_exercise: string; to_exercise: string; reason?: string }[]>([]);
  repBounds = signal<{ min_reps: number | null; max_reps: number | null; max_rpe: number | null; min_rir: number | null }>({
    min_reps: null,
    max_reps: null,
    max_rpe: null,
    min_rir: null,
  });
  frequency = signal<{ max_sessions_per_week: number | null; min_rest_days_between_sessions: number | null }>({
    max_sessions_per_week: null,
    min_rest_days_between_sessions: null,
  });
  volume = signal<{ max_sets_per_workout: number | null; max_sets_per_exercise: number | null }>({
    max_sets_per_workout: null,
    max_sets_per_exercise: null,
  });
  equipment = signal<{ available: string; prohibited: string }>({
    available: '',
    prohibited: '',
  });
  recovery = signal<{ min_sleep_hours: number | null; min_readiness_score: number | null; auto_deload_on_fatigue: boolean }>({
    min_sleep_hours: null,
    min_readiness_score: null,
    auto_deload_on_fatigue: true,
  });
  contextualNotes = signal<{ category: string; note: string; severity: string }[]>([]);

  ngOnInit() {
    this.loadSettings();
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

          const rules = res.training_rules || prefs.training_rules || null;
          if (rules) {
            this.trainingRules.set(rules);
            this.prohibitedExercises.set(rules.prohibited_exercises || []);
            this.substitutions.set(rules.substitutions || []);
            const b = rules.rep_intensity_bounds || {};
            this.repBounds.set({
              min_reps: b.min_reps ?? null,
              max_reps: b.max_reps ?? null,
              max_rpe: b.max_rpe ?? null,
              min_rir: b.min_rir ?? null,
            });
            const f = rules.frequency || {};
            this.frequency.set({
              max_sessions_per_week: f.max_sessions_per_week ?? null,
              min_rest_days_between_sessions: f.min_rest_days_between_sessions ?? null,
            });
            const v = rules.volume || {};
            this.volume.set({
              max_sets_per_workout: v.max_sets_per_workout ?? null,
              max_sets_per_exercise: v.max_sets_per_exercise ?? null,
            });
            const eq = rules.equipment || {};
            this.equipment.set({
              available: (eq.available_equipment || []).join(', '),
              prohibited: (eq.prohibited_equipment || []).join(', '),
            });
            const r = rules.recovery || {};
            this.recovery.set({
              min_sleep_hours: r.min_sleep_hours ?? null,
              min_readiness_score: r.min_readiness_score ?? null,
              auto_deload_on_fatigue: r.auto_deload_on_fatigue ?? true,
            });
            this.contextualNotes.set(rules.contextual_notes || []);
          }
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

  addProhibitedExercise() {
    this.prohibitedExercises.set([...this.prohibitedExercises(), { exercise: '', reason: '' }]);
  }

  removeProhibitedExercise(index: number) {
    this.prohibitedExercises.set(this.prohibitedExercises().filter((_, i) => i !== index));
  }

  addSubstitution() {
    this.substitutions.set([
      ...this.substitutions(),
      { from_exercise: '', to_exercise: '', reason: '' },
    ]);
  }

  removeSubstitution(index: number) {
    this.substitutions.set(this.substitutions().filter((_, i) => i !== index));
  }

  addContextualNote() {
    this.contextualNotes.set([
      ...this.contextualNotes(),
      { category: 'general', note: '', severity: 'info' },
    ]);
  }

  removeContextualNote(index: number) {
    this.contextualNotes.set(this.contextualNotes().filter((_, i) => i !== index));
  }

  buildTrainingRulesPayload() {
    const currentVersion = this.trainingRules()?.version || 1;
    const availEq = this.equipment().available
      ? this.equipment()
          .available.split(',')
          .map((s) => s.trim())
          .filter(Boolean)
      : [];
    const prohibEq = this.equipment().prohibited
      ? this.equipment()
          .prohibited.split(',')
          .map((s) => s.trim())
          .filter(Boolean)
      : [];

    return {
      version: currentVersion,
      prohibited_exercises: this.prohibitedExercises().filter((p) => p.exercise?.trim()),
      substitutions: this.substitutions().filter(
        (s) => s.from_exercise?.trim() && s.to_exercise?.trim(),
      ),
      rep_intensity_bounds: {
        min_reps: parseNullableNumber(this.repBounds().min_reps),
        max_reps: parseNullableNumber(this.repBounds().max_reps),
        max_rpe: parseNullableNumber(this.repBounds().max_rpe),
        min_rir: parseNullableNumber(this.repBounds().min_rir),
      },
      frequency: {
        max_sessions_per_week: parseNullableNumber(this.frequency().max_sessions_per_week),
        min_rest_days_between_sessions: parseNullableNumber(
          this.frequency().min_rest_days_between_sessions,
        ),
      },
      volume: {
        max_sets_per_workout: parseNullableNumber(this.volume().max_sets_per_workout),
        max_sets_per_exercise: parseNullableNumber(this.volume().max_sets_per_exercise),
      },
      equipment: {
        available_equipment: availEq,
        prohibited_equipment: prohibEq,
      },
      recovery: {
        min_sleep_hours: parseNullableNumber(this.recovery().min_sleep_hours),
        min_readiness_score: parseNullableNumber(this.recovery().min_readiness_score),
        auto_deload_on_fatigue: Boolean(this.recovery().auto_deload_on_fatigue),
      },
      contextual_notes: this.contextualNotes().filter((n) => n.note?.trim()),
    };
  }

  saveTrainingRules() {
    const payload = this.buildTrainingRulesPayload();
    this.http.post<{ rules: TrainingRulesData }>('/api/settings/rules', payload).subscribe({
      next: (res: { rules: TrainingRulesData }) => {
        this.trainingRules.set(res.rules);
        this.rulesStatus.set(`Rules saved (v${res.rules?.version || payload.version})!`);
        setTimeout(() => this.rulesStatus.set(''), 4000);
      },
      error: (err: HttpErrorResponse) => {
        const detail = (err.error as { detail?: string } | undefined)?.detail;
        this.rulesStatus.set(detail || 'Could not save training rules.');
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
      training_rules: this.buildTrainingRulesPayload(),
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
