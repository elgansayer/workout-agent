export type RoutineRole = 'required' | 'optional' | 'rotating';
export type CadenceMode = 'rolling' | 'weekly';
export type ConnectionState =
  | 'connected'
  | 'partial'
  | 'disconnected'
  | 'auth_error'
  | 'rate_limited'
  | 'error'
  | 'unknown';
export type RefreshState = 'ok' | 'partial' | 'auth_error' | 'rate_limited' | 'error';

export interface HevyFolder {
  id: number | string;
  title: string;
  routine_count: number;
}

export interface HevySetSummary {
  index: number;
  type: string;
  load_kg: number | null;
  reps: number | null;
  rep_range: { start: number; end: number } | null;
  distance_meters: number | null;
  duration_seconds: number | null;
  rpe: number | null;
  custom_targets: Record<string, unknown>;
  unsupported_fields: string[];
}

export interface HevyExercise {
  template_id: string;
  title: string;
  exercise_type: string;
  primary_muscle_group: string;
  secondary_muscle_groups: string[];
  equipment?: string | null;
  sets: number;
  set_targets: Record<string, unknown>[];
  set_summary: HevySetSummary[];
  set_types: string[];
  unsupported_fields: string[];
  rest_seconds: number;
  notes: string | null;
  superset_id: string | number | null;
  target_weight_kg?: number | null;
  target_reps?: number | null;
  target_rep_range?: [number, number] | null;
}

export interface HevyRoutine {
  id: string;
  title: string;
  folder_id: number | string | null;
  folder_name: string | null;
  updated_at: string | null;
  exercises: HevyExercise[];
  exercise_count: number;
  work_set_count: number;
  estimated_duration_minutes: number;
  muscle_summary: string[];
  unsupported_fields: string[];
  source_hash: string;
  last_performed_at?: string | null;
  recent_completion_count: number;
}

export interface HevySource {
  username: string | null;
  workout_count: number;
  routine_count: number;
  recent_workout_count: number;
  folders: HevyFolder[];
  routines: HevyRoutine[];
}

export interface HevyConnection {
  state: ConnectionState;
  detail: string | null;
  username: string | null;
  owner?: string | null;
  last_successful_sync?: string | null;
  stale?: boolean;
  warnings?: string[];
  counts?: Record<string, number>;
  fetched_at?: string | null;
}

export interface SourceSnapshot {
  id: string;
  title?: string;
  source_hash: string;
}

export interface ActiveProgrammeDefinition {
  name?: string | null;
  cycle_weeks?: number | null;
  total_days?: number | null;
  programme_spec?: {
    goal_label?: string | null;
    selected_routine_ids?: string[];
  } | null;
  source_snapshots?: SourceSnapshot[] | null;
}

export interface ActiveProgramme {
  source?: string;
  name?: string | null;
  definition?: ActiveProgrammeDefinition | null;
}

export interface GoalOption {
  key: string;
  label: string;
  description?: string;
}

export type ProgrammeDriftStatus =
  | 'no_active_programme'
  | 'unchanged'
  | 'review_recommended'
  | 'action_required'
  | 'source_unavailable';

export type ProgrammeDriftRoutineState =
  | 'renamed'
  | 'content_changed'
  | 'removed'
  | 'unchanged';

export interface ProgrammeDriftRoutine {
  position: number;
  routine_id: string;
  title: string;
  current_title: string | null;
  state: ProgrammeDriftRoutineState;
  source_exercise_count: number | null;
  current_exercise_count: number | null;
}

export interface ProgrammeSourceDrift {
  status: ProgrammeDriftStatus;
  has_changes: boolean;
  routines: ProgrammeDriftRoutine[];
  summary: {
    renamed: number;
    content_changed: number;
    removed: number;
  };
  detail?: string | null;
}

export interface BuilderData {
  active_programme: ActiveProgramme | null;
  templates: unknown[];
  connection: HevyConnection;
  source: HevySource;
  source_drift?: ProgrammeSourceDrift | null;
  duration: {
    minimum_weeks: number;
    maximum_weeks: number;
    default_weeks: number;
  };
  goals: GoalOption[];
  cadence: {
    modes: { key: string; label: string; description?: string }[];
    weekdays: { index: number; name: string }[];
    roles: { key: RoutineRole; label: string }[];
  };
}

export interface ProgrammeCadencePreview {
  mode: CadenceMode;
  training_days: number[];
  training_day_names: string[];
  sessions_per_week: number;
  rotation: {
    routine_id: string;
    title: string;
    role: RoutineRole;
    position: number;
    preferred_weekday: number | null;
    scheduled_weekdays: number[];
    scheduled_weekday_names: string[];
    scheduled: boolean;
  }[];
  unscheduled_routines: {
    routine_id: string;
    title: string;
    role: RoutineRole;
    reason: string;
  }[];
  warnings: string[];
}

export interface ProgrammeScheduleSession {
  week: number;
  date: string;
  weekday_index: number;
  weekday: string;
  routine_id: string;
  routine_title: string;
  rotation_position: number;
  role: RoutineRole;
}

export interface MuscleSetEstimate {
  muscle: string;
  weekly_sets: number;
  status: string;
}

export interface ProgrammeBlock {
  number: number;
  key: string;
  name: string;
  start_week: number;
  end_week: number;
  duration_weeks: number;
  weeks: string;
  start_date: string;
  end_date: string;
  focus: string;
  volume_multiplier: number;
  target_rir: string;
  rep_emphasis: string;
}

export interface ProgrammePrescription {
  block_number: number;
  sets: number;
  rep_range: string;
  target_rir: string;
  progression: {
    method: string;
    rule: string;
  };
}

export interface ProgrammeDayExercise {
  name: string;
  role: string;
  role_reason: string;
  scheme: string;
  prescriptions: ProgrammePrescription[];
}

export interface ProgrammeDay {
  number: number;
  routine_id: string;
  source_hash: string;
  focus: string;
  muscles: string[];
  estimated_duration_minutes: number;
  exercises: ProgrammeDayExercise[];
}

export interface ProgrammeViolation {
  code?: string;
  field?: string;
  routine_id?: string;
  message?: string;
  exercise?: string;
  prescription?: string;
  rule?: string;
  permitted_remediation?: string;
}

export interface ProgrammePreview {
  name: string;
  source: string;
  engine_version: string;
  preview_token: string;
  cycle_weeks: number;
  total_days: number;
  source_snapshots: HevyRoutine[];
  analysis: { confidence: string };
  cadence: ProgrammeCadencePreview;
  schedule: ProgrammeScheduleSession[];
  muscle_sets: {
    method: string;
    weekly_sets: Record<string, number>;
    estimates: MuscleSetEstimate[];
    warnings: string[];
  };
  drift: {
    state: string;
    changed_routines: string[];
  };
  blocks: ProgrammeBlock[];
  days: ProgrammeDay[];
  warnings: string[];
  blocking_violations: ProgrammeViolation[];
  assumptions: string[];
  rules: string[];
}

export interface ProgrammeRoutineSelection {
  routine_id: string;
  role: RoutineRole;
}

export interface ProgrammePreviewPayload {
  selected_routine_ids: string[];
  routine_selections: ProgrammeRoutineSelection[];
  duration_weeks: number;
  goal: string;
  start_date: string;
  sessions_per_week: number | null;
  cadence: {
    mode: CadenceMode;
    weekdays: number[];
  };
  experience: string;
  max_session_minutes: number | null;
  adaptation_aggressiveness: string;
  priority_muscles: string[];
  priority_exercises: string[];
  event_date: string | null;
  fatigue_management: string;
  constraints: {
    concurrent_training: string[];
    injuries: string[];
    notes: string | null;
    max_sessions_per_week: number | null;
  };
}

export interface ProgrammeActivationPayload extends ProgrammePreviewPayload {
  preview_token: string;
  source_hashes: { routine_id: string; source_hash: string }[];
}

export interface PreviewResponse {
  preview: ProgrammePreview;
}

export interface RefreshResponse {
  state: RefreshState;
  detail?: string | null;
  source?: HevySource | null;
  counts?: Record<string, number>;
  warnings?: string[];
  last_successful_sync?: string | null;
  stale?: boolean;
}

export interface ConflictRoutine {
  routine_id: string;
  title?: string | null;
  previous_source_hash?: string | null;
  current_source_hash?: string | null;
}

export interface SourceConflict {
  changed_routines: ConflictRoutine[];
  added_routines: ConflictRoutine[];
  removed_routines: ConflictRoutine[];
  has_conflicts: boolean;
}

export interface ProgrammeErrorDetail {
  message?: string;
  conflict?: SourceConflict;
  remediation?: string[];
  violations?: ProgrammeViolation[];
}
