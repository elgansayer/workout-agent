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
}

/** Status of one platform connector. */
export interface ConnectorInfo {
  readonly provider: string;
  readonly name: string;
  readonly state: string;
  readonly message: string;
  readonly authorize_supported: boolean;
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
  readonly vapid_public_key: string | null;
  readonly key_status?: string | null;
  readonly pref_status?: string | null;
}
