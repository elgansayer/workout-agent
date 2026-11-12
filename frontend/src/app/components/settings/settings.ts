import { Component, computed, inject, signal, OnInit, OnDestroy } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { DisplaySettings, DisplaySettingsService } from '../../services/display-settings';
import { SetupAccount, SetupStateService } from '../../services/setup-state.service';
import { aiProviderFact, hevyFact, keyVerification, lastSyncFact } from './settings.readiness';
import {
  CredentialTestResult,
  EquipmentRule,
  FrequencyRule,
  HevyConnection,
  HevyVerificationResult,
  ProgrammeConstraintViolation,
  ProgrammeReconciliation,
  RecoveryRule,
  RepIntensityBoundsRule,
  SaveKeyResult,
  SettingsResponse,
  SyncHistoryResult,
  TrainingRulesData,
  TrainingRulesSaveResponse,
  VolumeRule,
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

function parseNullableNumber(val: unknown): number | null {
  if (val === null || val === undefined || val === '') {
    return null;
  }
  const num = Number(val);
  return Number.isNaN(num) ? null : num;
}

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

interface ConnectorSummary {
  connection_id?: string | null;
  provider: string;
  name?: string;
}

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './settings.html',
  styleUrl: './settings.css',
})
export class Settings implements OnInit, OnDestroy {
  private http = inject(HttpClient);
  protected readonly display = inject(DisplaySettingsService);
  private readonly setupState = inject(SetupStateService);

  /** True while this tab is waiting for a Hevy sync it started. */
  readonly syncing = signal(false);
  /** True while this tab or any other is running a Hevy sync or connection check. */
  readonly connectorBusy = computed(
    () => this.syncing() || !!this.setupState.account()?.connector?.operation,
  );
  readonly readinessFailed = this.setupState.failed;
  /**
   * The server-derived readiness of this account, shared with the navigation.
   * While this tab's own sync is in flight the panel says so itself, instead of
   * showing the snapshot taken before the sync started as if it were current.
   */
  readonly readiness = computed<SetupAccount | null>(() => {
    const account = this.setupState.account();
    if (!account || !this.syncing()) return account;
    return {
      ...account,
      state: 'syncing',
      label: 'Syncing your Hevy history',
      reason:
        'A Hevy sync is running. This view updates when it finishes, and figures may be incomplete until then.',
      reason_code: 'syncing',
      connector: {
        provider: account.connector?.provider ?? 'hevy',
        operation_started_at: account.connector?.operation_started_at ?? null,
        operation_stalled: false,
        last_sync: account.connector?.last_sync ?? null,
        operation: 'syncing',
      },
    };
  });

  data = signal<SettingsResponse | null>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);

  section = signal('connections');
  pushStatus = signal('');
  healthStatus = signal('');
  disconnectingHealth = signal(false);
  retentionMode = signal('revoke_only');
  connectorRetention: { [provider: string]: string } = {};
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

  deletion = signal<DeletionInfo | null>(null);
  deletionStatus = signal('');
  deletionError = signal('');
  deletionBusy = signal(false);
  healthConfirmation = '';
  accountConfirmation = '';
  private deletionPoll?: ReturnType<typeof setTimeout>;

  trainingRules = signal<TrainingRulesData | null>(null);
  rulesStatus = signal<string>('');
  ruleViolations = signal<ProgrammeConstraintViolation[]>([]);
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

  displayDraft = signal<DisplaySettings>({ ...this.display.settings() });
  displayStatus = signal<string>('');
  displayStatusIsError = signal<boolean>(false);
  displaySaving = signal<boolean>(false);

  ngOnInit() {
    this.loadSettings();
    void this.loadDisplay();
    // Deep links from the Account page and first-run setup: /settings?section=...
    const requested = new URLSearchParams(window.location.search).get('section');
    if (
      requested &&
      ['connections', 'ai', 'profile', 'display', 'notifications', 'account'].includes(
        requested,
      )
    ) {
      this.selectSection(requested);
    }
  }

  ngOnDestroy() {
    this.stopDeletionPoll();
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

  selectSection(name: string) {
    this.section.set(name);
    // Deletion status is only read when the section is opened.
    if (name === 'account') this.loadDeletion();
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

          const rules = res.training_rules || prefs.training_rules || null;
          if (rules) {
            this.trainingRules.set(rules);
            this.prohibitedExercises.set(rules.prohibited_exercises || []);
            this.substitutions.set(rules.substitutions || []);
            const b: RepIntensityBoundsRule = rules.rep_intensity_bounds ?? { min_reps: null, max_reps: null, max_rpe: null, min_rir: null };
            this.repBounds.set({
              min_reps: b.min_reps ?? null,
              max_reps: b.max_reps ?? null,
              max_rpe: b.max_rpe ?? null,
              min_rir: b.min_rir ?? null,
            });
            const f: FrequencyRule = rules.frequency ?? { max_sessions_per_week: null, min_rest_days_between_sessions: null };
            this.frequency.set({
              max_sessions_per_week: f.max_sessions_per_week ?? null,
              min_rest_days_between_sessions: f.min_rest_days_between_sessions ?? null,
            });
            const v: VolumeRule = rules.volume ?? { max_sets_per_workout: null, max_sets_per_exercise: null };
            this.volume.set({
              max_sets_per_workout: v.max_sets_per_workout ?? null,
              max_sets_per_exercise: v.max_sets_per_exercise ?? null,
            });
            const eq: EquipmentRule = rules.equipment ?? { available_equipment: [], prohibited_equipment: [] };
            this.equipment.set({
              available: (eq.available_equipment || []).join(', '),
              prohibited: (eq.prohibited_equipment || []).join(', '),
            });
            const r: RecoveryRule = rules.recovery ?? { min_sleep_hours: null, min_readiness_score: null, auto_deload_on_fatigue: true };
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

  readonly hevyFact = hevyFact;
  readonly lastSyncFact = lastSyncFact;
  readonly aiProviderFact = aiProviderFact;

  /** How a stored AI key is labelled from its last connection test. */
  aiKeyState(provider: string) {
    const key = this.data()?.user_keys?.[provider];
    return keyVerification(key?.has_key, key?.last_test_status, key?.last_tested_at);
  }

  /** Re-read the account's readiness after something that can change it. */
  private refreshReadiness(): void {
    this.setupState.load();
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
          this.aiStatus.set(`${provider} key saved. Use Test to confirm it works.`);
          this.aiKeyInputs[provider] = '';
        }
        this.loadSettings(false);
        this.refreshReadiness();
      },
      error: (err: unknown) => {
        const msg = errorDetail(err) ?? 'Error saving key.';
        if (provider === 'hevy') this.hevyStatus.set(msg);
        else this.aiStatus.set(msg);
      },
    });
  }

  private setProviderStatus(provider: string, message: string) {
    if (provider === 'hevy') this.hevyStatus.set(message);
    else this.aiStatus.set(message);
  }

  testKey(provider: string) {
    const key =
      provider === 'hevy' ? this.hevyKeyInput.trim() : (this.aiKeyInputs[provider] || '').trim();
    const model = (this.aiModelInputs[provider] || '').trim();
    this.setProviderStatus(provider, 'Testing credential...');
    this.http
      .post<CredentialTestResult>('/api/settings/key/test', {
        provider,
        api_key: key || '__stored__',
        model: model || undefined,
      })
      .subscribe({
        next: (res) => {
          if (res.status === 'ok') {
            const count = typeof res.workout_count === 'number' ? ` ${res.workout_count} workouts found.` : '';
            this.setProviderStatus(provider, `✓ Connected.${count}`);
          } else {
            this.setProviderStatus(provider, res.detail || 'Verification failed.');
          }
          // The server records the outcome either way; show what it recorded.
          this.loadSettings(false);
          this.refreshReadiness();
        },
        error: (err: unknown) => {
          this.setProviderStatus(provider, errorDetail(err) ?? 'Verification failed.');
          this.refreshReadiness();
        },
      });
  }

  deleteKey(provider: string) {
    if (!confirm(`Remove your ${provider} API key?`)) return;
    this.http.post<{ status: string; provider: string }>('/api/settings/key/delete', { provider }).subscribe({
      next: () => {
        this.loadSettings(false);
        this.refreshReadiness();
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
          this.refreshReadiness();
        }
      },
      error: (err: unknown) => {
        this.hevyStatus.set(errorDetail(err) ?? 'Could not verify the Hevy API key.');
        if (!candidate) {
          // A stored key Hevy now rejects is recorded; show what was recorded.
          this.loadSettings(false);
        }
        this.refreshReadiness();
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

  revokeKey(provider: string) {
    if (!confirm(`Revoke your ${provider} credential? Background jobs will stop using it.`)) return;
    this.http.post('/api/settings/key/revoke', { provider }).subscribe({
      next: () => {
        this.setProviderStatus(provider, 'Credential revoked.');
        this.loadSettings(false);
        this.refreshReadiness();
      },
      error: (err: unknown) => {
        this.setProviderStatus(provider, errorDetail(err) ?? 'Could not revoke credential.');
      },
    });
  }

  syncHistory() {
    if (this.connectorBusy()) return;
    if (!confirm('This will rebuild your workout history from Hevy. Continue?')) return;
    this.syncing.set(true);
    this.hevyStatus.set('Syncing workout history from Hevy...');
    this.http.post<SyncHistoryResult>('/api/settings/sync-history', {}).subscribe({
      next: (res) => {
        this.hevyStatus.set(
          `✓ Synced! ${res.processed} of ${res.workouts_found} workouts processed.`,
        );
        this.syncing.set(false);
        this.refreshReadiness();
      },
      error: (err: unknown) => {
        this.hevyStatus.set(errorDetail(err) ?? 'Sync failed.');
        this.syncing.set(false);
        this.refreshReadiness();
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
    this.http
      .post<TrainingRulesSaveResponse>('/api/settings/rules', payload)
      .subscribe({
        next: (res: TrainingRulesSaveResponse) => {
          this.trainingRules.set(res.rules);
          this.applyReconciliation(res);
          if (this.ruleViolations().length === 0) {
            this.rulesStatus.set(
              `Rules saved (v${res.rules?.version || payload.version})!`,
            );
            setTimeout(() => this.rulesStatus.set(''), 4000);
          }
        },
        error: (err: HttpErrorResponse) => {
          const detail = (err.error as { detail?: string } | undefined)?.detail;
          this.rulesStatus.set(detail || 'Could not save training rules.');
        },
      });
  }

  private applyReconciliation(res: {
    blocking_violations?: ProgrammeConstraintViolation[];
    reconciliation?: ProgrammeReconciliation;
  }): void {
    const violations = res.blocking_violations ?? [];
    this.ruleViolations.set(violations);
    if (violations.length > 0) {
      this.rulesStatus.set(
        res.reconciliation?.message ||
          'The active programme violates the saved constraints.',
      );
    }
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

    this.http
      .post<{
        status: string;
        blocking_violations?: ProgrammeConstraintViolation[];
        reconciliation?: ProgrammeReconciliation;
      }>('/api/settings/preferences', payload)
      .subscribe({
        next: (res) => {
          this.applyReconciliation(res);
          if (this.ruleViolations().length === 0) {
            this.prefStatus.set('Profile saved!');
            setTimeout(() => this.prefStatus.set(''), 4000);
          } else {
            this.prefStatus.set(
              res.reconciliation?.message ||
                'The active programme violates the saved constraints.',
            );
          }
        },
        error: (err: unknown) => {
          this.prefStatus.set(errorDetail(err) ?? 'Could not save preferences.');
        },
      });
  }

  private confirmRetention(provider: string, mode: string): boolean {
    if (mode === 'purge') {
      return confirm(
        `Disconnect ${provider} and permanently delete the data it imported plus ` +
          'insights derived from it? Workouts and check-ins you entered are kept.',
      );
    }
    return true;
  }

  disconnectGoogleHealth(): void {
    if (this.disconnectingHealth()) return;
    const mode = this.retentionMode();
    if (!this.confirmRetention('Google Health', mode)) return;
    this.disconnectingHealth.set(true);
    this.http
      .post(
        '/google-health/disconnect',
        { mode, confirmed: mode === 'purge' },
        { responseType: 'text' },
      )
      .subscribe({
        next: () => {
          this.disconnectingHealth.set(false);
          this.healthStatus.set(
            mode === 'purge'
              ? 'Google Health disconnected and imported data purged.'
              : 'Google Health disconnected.',
          );
          this.loadSettings(false);
        },
        error: () => {
          this.disconnectingHealth.set(false);
          this.healthStatus.set('Could not disconnect Google Health. Please try again.');
        },
      });
  }

  setConnectorRetention(provider: string, mode: string): void {
    this.connectorRetention[provider] = mode;
  }

  disconnectConnector(connector: ConnectorSummary): void {
    if (!connector?.connection_id) return;
    const mode = this.connectorRetention[connector.provider] || 'revoke_only';
    if (!this.confirmRetention(connector.name || connector.provider, mode)) return;
    this.http
      .delete(`/api/health/connections/${encodeURIComponent(connector.connection_id)}`, {
        params: { mode, confirmed: String(mode === 'purge') },
        responseType: 'text',
      })
      .subscribe({
        next: () => {
          this.healthStatus.set(
            mode === 'purge'
              ? `${connector.name} disconnected and imported data purged.`
              : `${connector.name} disconnected.`,
          );
          this.loadSettings(false);
        },
        error: () => {
          this.healthStatus.set(`Could not disconnect ${connector.name}. Please try again.`);
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
