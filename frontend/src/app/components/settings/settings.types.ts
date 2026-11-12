/**
 * Typed contracts for the settings screen.
 *
 * The settings API is the only source of truth for what is stored for a
 * user, so every shape it returns is described here instead of being treated
 * as an untyped blob. The Hevy contract is deliberately credential free: it
 * carries the connected identity and the verification metadata, never the
 * API key itself.
 */

/** Outcome of the last Hevy credential verification. */
export type HevyConnectionState =
  | 'connected'
  | 'invalid'
  | 'insufficient_scope'
  | 'unverified'
  | 'not_connected';

/** Safe metadata describing a verified Hevy connection. */
export interface HevyConnection {
  readonly state: HevyConnectionState;
  readonly account_username: string | null;
  readonly account_id: string | null;
  readonly api_version: string | null;
  readonly capabilities: Readonly<Record<string, boolean>>;
  readonly tested_at: string | null;
}

/** Result of a Hevy credential test performed against the API. */
export interface HevyVerificationResult extends HevyConnection {
  readonly status: string;
  readonly workout_count: number | null;
}

/** Result of storing a provider key. */
export interface SaveKeyResult {
  readonly status: string;
  readonly provider: string;
  readonly connection?: HevyConnection;
}

/** Result of testing a stored or candidate provider credential. */
export interface CredentialTestResult {
  readonly status: string;
  readonly detail?: string;
  readonly workout_count?: number | null;
}

/** Result of a history sync request. */
export interface SyncHistoryResult {
  readonly processed: number;
  readonly workouts_found: number;
}

/** Whether credential storage is usable on this server. */
export interface CredentialVaultAvailability {
  readonly available: boolean;
  readonly reason: string | null;
}

/** One audited credential lifecycle event. Carries no secret material. */
export interface CredentialAuditEvent {
  readonly id: number;
  readonly provider: string;
  readonly action: string;
  readonly outcome: string;
  readonly detail: string | null;
  readonly actor: string;
  readonly source: string;
  readonly version: number | null;
  readonly created_at: string;
}

/** One AI provider advertised by the provider registry. */
export interface AiProviderInfo {
  readonly id: string;
  readonly name: string;
  readonly default_model: string;
}

/** Whether a key is stored for one provider, without exposing the key. */
export interface StoredKeyInfo {
  readonly has_key: boolean;
  readonly masked: string | null;
  readonly model: string | null;
  readonly status?: string;
  /** Outcome of the last connection test: `ok`, a failure code, or null if never tested. */
  readonly last_test_status?: string | null;
  readonly last_tested_at?: string | null;
}

/**
 * Stored keys per provider. `hevy` is declared explicitly because the Hevy
 * section always renders; every other provider is looked up by id.
 */
export type StoredKeys = {
  readonly hevy?: StoredKeyInfo;
} & {
  readonly [provider: string]: StoredKeyInfo | undefined;
};

/** The user's training preferences. */
export interface UserPreferences {
  readonly goals: readonly string[];
  readonly constraints: readonly string[];
  readonly experience_level: string;
  readonly coaching_style: string;
  readonly preferred_ai: string;
  readonly ai_model: string | null;
  readonly custom_rules: readonly string[];
  readonly training_rules?: TrainingRulesData | null;
}

/** One explicit choice for what happens to imported data on disconnect. */
export interface RetentionOption {
  readonly mode: 'revoke_only' | 'retain' | 'purge';
  readonly label: string;
  readonly explanation: string;
  readonly deletes_imported_data: boolean;
  readonly deletes_derived_data: boolean;
}

/** Status of one platform connector. */
export interface ConnectorInfo {
  readonly provider: string;
  readonly name: string;
  readonly state: string;
  readonly message: string | null;
  readonly authorize_supported: boolean;
  readonly purge_supported?: boolean;
  readonly connection_id?: string | null;
  readonly retention_options?: readonly RetentionOption[];
}

/** An exercise that must never be prescribed or performed. */
export interface ProhibitedExerciseRule {
  exercise: string;
  reason?: string;
}

/** An explicit replacement of one exercise with another. */
export interface ExerciseSubstitutionRule {
  from_exercise: string;
  to_exercise: string;
  reason?: string;
}

/** Bounds on repetitions, RPE, RIR and intensity. */
export interface RepIntensityBoundsRule {
  min_reps: number | null;
  max_reps: number | null;
  max_rpe: number | null;
  min_rir: number | null;
}

/** Scheduling and frequency limits. */
export interface FrequencyRule {
  max_sessions_per_week: number | null;
  min_rest_days_between_sessions: number | null;
}

/** Volume limits. */
export interface VolumeRule {
  max_sets_per_workout: number | null;
  max_sets_per_exercise: number | null;
}

/** Available and prohibited equipment. */
export interface EquipmentRule {
  available_equipment: string[];
  prohibited_equipment: string[];
}

/** Recovery and readiness thresholds. */
export interface RecoveryRule {
  min_sleep_hours: number | null;
  min_readiness_score: number | null;
  auto_deload_on_fatigue: boolean;
}

/** A contextual medical, joint or general note. */
export interface ContextualNoteRule {
  category: string;
  note: string;
  severity: string;
}

/** Versioned, user-owned structured training rules document. */
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

/** A deterministic programme constraint violation with exact remediation. */
export interface ProgrammeConstraintViolation {
  exercise: string;
  prescription: string;
  rule: string;
  permitted_remediation: string;
  remediation?: string;
  block_number?: number | null;
  routine?: string | null;
}

/** Active-programme reconciliation result after profile or rule edits. */
export interface ProgrammeReconciliation {
  status: 'reconciled' | 'requires_remediation';
  message?: string;
}

/** The `/api/settings/rules` response. */
export interface TrainingRulesSaveResponse {
  status: string;
  rules: TrainingRulesData;
  blocking_violations?: ProgrammeConstraintViolation[];
  reconciliation?: ProgrammeReconciliation;
}

/** The `/api/settings` response. */
export interface SettingsResponse {
  readonly user_prefs: UserPreferences;
  readonly user_keys: StoredKeys;
  readonly hevy_connection: HevyConnection;
  readonly credential_vault: CredentialVaultAvailability;
  readonly credential_events: readonly CredentialAuditEvent[];
  readonly ai_providers: readonly AiProviderInfo[];
  readonly gh_connected: boolean;
  readonly gh_configured: boolean;
  readonly gh_status?: string | null;
  readonly connectors_info: readonly ConnectorInfo[];
  readonly retention_options: readonly RetentionOption[];
  readonly vapid_public_key: string | null;
  readonly key_status?: string | null;
  readonly pref_status?: string | null;
  readonly training_rules?: TrainingRulesData | null;
}
