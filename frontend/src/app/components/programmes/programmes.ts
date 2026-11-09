import { CommonModule } from '@angular/common';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { HlmAlertImports } from '@spartan-ng/ui/alert';
import { HlmBadgeImports } from '@spartan-ng/ui/badge';
import { HlmButtonImports } from '@spartan-ng/ui/button';
import { HlmCardImports } from '@spartan-ng/ui/card';
import { HevyState } from './hevy-state';
import type {
  BuilderData,
  CadenceMode,
  HevyConnection,
  HevyRoutine,
  PreviewResponse,
  ProgrammeActivationPayload,
  ProgrammeErrorDetail,
  ProgrammePreview as ProgrammePreviewData,
  ProgrammePreviewPayload,
  ProgrammeSourceDrift,
  ProgrammeViolation,
  RefreshResponse,
  RefreshState,
  RoutineRole,
  SourceConflict,
} from './programme-contracts';
import { ProgrammePreview } from './programme-preview';
import { SetTargets } from './set-targets';

interface RoutineGroup {
  key: string;
  title: string;
  routines: HevyRoutine[];
}

const WEEKDAYS = [
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
  'Sunday',
];

const ROLE_LABELS: Record<RoutineRole, string> = {
  required: 'Required',
  optional: 'Optional',
  rotating: 'Rotating',
};

@Component({
  selector: 'app-programmes',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    HlmCardImports,
    HlmButtonImports,
    HlmBadgeImports,
    HlmAlertImports,
    HevyState,
    ProgrammePreview,
    SetTargets,
  ],
  templateUrl: './programmes.html',
  styleUrl: './programmes.css',
})
export class Programmes implements OnInit {
  private readonly http = inject(HttpClient);

  readonly weekdays = WEEKDAYS;
  readonly roleLabels = ROLE_LABELS;

  data = signal<BuilderData | null>(null);
  preview = signal<ProgrammePreviewData | null>(null);
  loading = signal(true);
  refreshing = signal(false);
  previewing = signal(false);
  activating = signal(false);
  error = signal<string | null>(null);
  status = signal<string | null>(null);
  violations = signal<ProgrammeViolation[]>([]);
  conflict = signal<SourceConflict | null>(null);
  remediation = signal<string[]>([]);
  driftDismissed = signal(false);

  selectedRoutineIds = signal<string[]>([]);
  routineRoles = signal<Record<string, RoutineRole>>({});

  searchQuery = signal('');
  folderFilter = signal('');
  page = signal(1);
  readonly pageSize = 5;

  durationWeeks = 12;
  goal = 'general_fitness';
  startDate = new Date().toISOString().slice(0, 10);
  sessionsPerWeek: number | null = 3;
  experience = 'intermediate';
  maxSessionMinutes: number | null = null;
  adaptationAggressiveness = 'balanced';
  cadenceMode: CadenceMode = 'rolling';
  cadenceWeekdays = signal<number[]>([0, 2, 4]);
  priorityMuscles = '';
  priorityExercises = '';
  eventDate = '';
  fatigueManagement = 'planned';
  concurrentTraining = '';
  injuryConstraints = '';
  constraintNotes = '';
  maxSessionsPerWeek: number | null = null;

  readonly matchingRoutines = computed(() => {
    const query = this.searchQuery().trim().toLocaleLowerCase();
    const folder = this.folderFilter();
    const routines = this.data()?.source?.routines ?? [];
    return routines.filter((routine) => {
      if (folder !== '' && String(routine.folder_id ?? '') !== folder) {
        return false;
      }
      if (!query) {
        return true;
      }
      const haystack = [
        routine.title,
        routine.folder_name,
        ...(routine.muscle_summary ?? []),
        ...(routine.exercises ?? []).map((exercise) => exercise.title),
      ]
        .join(' ')
        .toLocaleLowerCase();
      return haystack.includes(query);
    });
  });

  readonly routineGroups = computed<RoutineGroup[]>(() => {
    const groups: RoutineGroup[] = [];
    const index = new Map<string, RoutineGroup>();
    for (const routine of this.matchingRoutines()) {
      const key = routine.folder_id === null || routine.folder_id === undefined
        ? 'unfiled'
        : String(routine.folder_id);
      let group = index.get(key);
      if (!group) {
        group = {
          key,
          title: routine.folder_name || 'Without a Hevy folder',
          routines: [],
        };
        index.set(key, group);
        groups.push(group);
      }
      group.routines.push(routine);
    }
    return groups;
  });

  readonly groupPageCount = computed(() =>
    Math.max(1, Math.ceil(this.routineGroups().length / this.pageSize)),
  );

  readonly visibleGroups = computed(() => {
    const start = (this.page() - 1) * this.pageSize;
    return this.routineGroups().slice(start, start + this.pageSize);
  });

  readonly selectedRoutines = computed<HevyRoutine[]>(() => {
    const routines = this.data()?.source?.routines ?? [];
    return this.selectedRoutineIds()
      .map((id) => routines.find((routine) => routine.id === id))
      .filter((routine): routine is HevyRoutine => routine !== undefined);
  });

  readonly connectionState = computed(() => this.data()?.connection?.state ?? 'unknown');

  readonly activeSourceHashes = computed(() => {
    const snapshots = this.data()?.active_programme?.definition?.source_snapshots ?? [];
    const hashes = new Map<string, string>();
    for (const snapshot of snapshots) {
      hashes.set(String(snapshot.id), String(snapshot.source_hash ?? ''));
    }
    return hashes;
  });

  /** Compare the live Hevy revision with the revision the active programme used. */
  driftFor(routine: HevyRoutine | null | undefined): 'unchanged' | 'changed' | 'added' | null {
    const hashes = this.activeSourceHashes();
    if (!hashes.size || !routine) {
      return null;
    }
    const previous = hashes.get(String(routine.id));
    if (previous === undefined) {
      return 'added';
    }
    return previous === routine.source_hash ? 'unchanged' : 'changed';
  }

  ngOnInit(): void {
    this.loadProgrammes();
  }

  loadProgrammes(refresh = false, preserveStatus = false): void {
    this.error.set(null);
    this.violations.set([]);
    this.conflict.set(null);
    this.remediation.set([]);
    if (!preserveStatus) this.status.set(null);
    this.preview.set(null);
    if (refresh) {
      this.refreshing.set(true);
    } else {
      this.loading.set(true);
    }

    this.http.get<BuilderData>('/api/programmes').subscribe({
      next: (data) => {
        this.data.set(data);
        this.reconcileSelection(data);
        this.loading.set(false);
        this.refreshing.set(false);
      },
      error: (err: unknown) => {
        this.error.set(this.describeError(err, 'Failed to load your Hevy routine library.'));
        this.loading.set(false);
        this.refreshing.set(false);
      },
    });
  }

  refreshFromHevy(): void {
    if (this.refreshing()) {
      return;
    }
    this.error.set(null);
    this.status.set(null);
    this.refreshing.set(true);
    this.http.post<RefreshResponse>('/api/programmes/refresh', {}).subscribe({
      next: (response) => {
        this.refreshing.set(false);
        const state = response.state;
        if (state === 'ok') {
          this.status.set('Hevy routines refreshed from your account.');
        } else if (state === 'partial') {
          this.status.set(
            'Hevy refreshed only part of your account. Review the warnings before building.',
          );
        } else if (state === 'auth_error') {
          this.error.set('Hevy rejected your API key. Update it in Settings and refresh again.');
        } else if (state === 'rate_limited') {
          this.error.set('Hevy is rate limiting requests. Try again in a few minutes.');
        } else {
          this.error.set(response.detail ?? 'Hevy could not be reached. Try again shortly.');
        }
        const current = this.data();
        if (!current) {
          return;
        }
        const connection: HevyConnection = {
          ...current.connection,
          state: this.connectionStateForRefresh(state),
          detail: response.detail ?? current.connection.detail ?? null,
          warnings: response.warnings ?? current.connection.warnings ?? [],
          counts: response.counts ?? current.connection.counts ?? {},
          last_successful_sync:
            response.last_successful_sync ?? current.connection.last_successful_sync ?? null,
          stale: response.stale ?? current.connection.stale ?? false,
        };
        this.data.set({
          ...current,
          ...(response.source ? { source: response.source } : {}),
          connection,
        });
        this.reconcileSelection(this.data());
      },
      error: (err: unknown) => {
        this.refreshing.set(false);
        this.error.set(this.describeError(err, 'Could not refresh from Hevy.'));
      },
    });
  }

  private connectionStateForRefresh(state: RefreshState): HevyConnection['state'] {
    return state === 'ok' ? 'connected' : state;
  }

  private reconcileSelection(data: BuilderData | null): void {
    const available = new Set((data?.source?.routines ?? []).map((routine) => routine.id));
    const activeIds: string[] =
      data?.active_programme?.definition?.programme_spec?.selected_routine_ids ?? [];
    const current = this.selectedRoutineIds().filter((id) => available.has(id));
    if (!current.length && activeIds.length) {
      this.selectedRoutineIds.set(activeIds.filter((id) => available.has(id)));
    } else if (current.length !== this.selectedRoutineIds().length) {
      this.selectedRoutineIds.set(current);
    }
    const roles = { ...this.routineRoles() };
    for (const id of this.selectedRoutineIds()) {
      if (!roles[id]) {
        roles[id] = 'required';
      }
    }
    this.routineRoles.set(roles);
  }

  setSearch(query: string): void {
    this.searchQuery.set(query);
    this.page.set(1);
  }

  filterFolder(folder: string): void {
    this.folderFilter.set(folder);
    this.page.set(1);
  }

  nextGroupPage(): void {
    this.page.set(Math.min(this.groupPageCount(), this.page() + 1));
  }

  previousGroupPage(): void {
    this.page.set(Math.max(1, this.page() - 1));
  }

  sourceDrift(): ProgrammeSourceDrift | null {
    const drift: ProgrammeSourceDrift | null | undefined = this.data()?.source_drift;
    if (!drift || this.driftDismissed()) {
      return null;
    }
    return drift;
  }

  driftNeedsAttention(): boolean {
    const drift = this.sourceDrift();
    return !!drift && (drift.has_changes || drift.status === 'source_unavailable');
  }

  keepCurrentProgramme(): void {
    this.driftDismissed.set(true);
    this.status.set(
      'Keeping the active programme. Hevy source changes are not applied until you generate a new preview.',
    );
  }

  reviewSourceChanges(): void {
    this.generatePreview();
  }

  isSelected(routineId: string): boolean {
    return this.selectedRoutineIds().includes(routineId);
  }

  routineById(routineId: string): HevyRoutine | null {
    return (
      (this.data()?.source?.routines ?? []).find(
        (routine) => routine.id === routineId,
      ) ?? null
    );
  }

  toggleRoutine(routineId: string): void {
    this.invalidatePreview();
    const selected = this.selectedRoutineIds();
    if (selected.includes(routineId)) {
      this.selectedRoutineIds.set(selected.filter((id) => id !== routineId));
      return;
    }
    this.selectedRoutineIds.set([...selected, routineId]);
    this.routineRoles.update((roles) => ({ ...roles, [routineId]: roles[routineId] ?? 'required' }));
  }

  roleFor(routineId: string): RoutineRole {
    return this.routineRoles()[routineId] ?? 'required';
  }

  setRole(routineId: string, role: RoutineRole): void {
    this.invalidatePreview();
    this.routineRoles.update((roles) => ({ ...roles, [routineId]: role }));
  }

  moveRoutine(index: number, delta: number): void {
    const nextIndex = index + delta;
    const selected = [...this.selectedRoutineIds()];
    if (nextIndex < 0 || nextIndex >= selected.length) {
      return;
    }
    [selected[index], selected[nextIndex]] = [selected[nextIndex], selected[index]];
    this.selectedRoutineIds.set(selected);
    this.invalidatePreview();
  }

  onRoutineKeydown(event: KeyboardEvent, index: number): void {
    if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') {
      return;
    }
    if (!event.ctrlKey && !event.metaKey && !event.altKey) {
      return;
    }
    event.preventDefault();
    this.moveRoutine(index, event.key === 'ArrowUp' ? -1 : 1);
  }

  removeRoutine(routineId: string): void {
    this.selectedRoutineIds.set(this.selectedRoutineIds().filter((id) => id !== routineId));
    this.invalidatePreview();
  }

  toggleWeekday(day: number): void {
    this.invalidatePreview();
    const days = this.cadenceWeekdays();
    const next = days.includes(day) ? days.filter((value) => value !== day) : [...days, day].sort();
    this.cadenceWeekdays.set(next);
    this.sessionsPerWeek = next.length;
  }

  isWeekdaySelected(day: number): boolean {
    return this.cadenceWeekdays().includes(day);
  }

  setCadenceMode(mode: CadenceMode): void {
    this.invalidatePreview();
    this.cadenceMode = mode;
    if (mode === 'weekly' && !this.cadenceWeekdays().length) {
      this.cadenceWeekdays.set([0, 2, 4]);
    }
    this.sessionsPerWeek = this.cadenceWeekdays().length;
  }

  invalidatePreview(): void {
    this.preview.set(null);
    this.violations.set([]);
  }

  private splitList(value: string): string[] {
    return value
      .split(',')
      .map((item) => item.trim())
      .filter((item) => item.length > 0);
  }

  buildPayload(): ProgrammePreviewPayload {
    return {
      selected_routine_ids: this.selectedRoutineIds(),
      routine_selections: this.selectedRoutineIds().map((id) => ({
        routine_id: id,
        role: this.roleFor(id),
      })),
      duration_weeks: Number(this.durationWeeks),
      goal: this.goal,
      start_date: this.startDate,
      sessions_per_week: this.sessionsPerWeek === null ? null : Number(this.sessionsPerWeek),
      cadence: {
        mode: this.cadenceMode,
        weekdays: this.cadenceWeekdays(),
      },
      experience: this.experience,
      max_session_minutes: this.maxSessionMinutes === null ? null : Number(this.maxSessionMinutes),
      adaptation_aggressiveness: this.adaptationAggressiveness,
      priority_muscles: this.splitList(this.priorityMuscles),
      priority_exercises: this.splitList(this.priorityExercises),
      event_date: this.eventDate || null,
      fatigue_management: this.fatigueManagement,
      constraints: {
        concurrent_training: this.splitList(this.concurrentTraining),
        injuries: this.splitList(this.injuryConstraints),
        notes: this.constraintNotes.trim() ? this.constraintNotes.trim() : null,
        max_sessions_per_week:
          this.maxSessionsPerWeek === null ? null : Number(this.maxSessionsPerWeek),
      },
    };
  }

  generatePreview(): void {
    if (
      !this.selectedRoutineIds().length ||
      this.previewing() ||
      this.activating() ||
      this.refreshing()
    ) {
      return;
    }
    this.error.set(null);
    this.status.set(null);
    this.violations.set([]);
    this.conflict.set(null);
    this.remediation.set([]);
    this.previewing.set(true);
    this.preview.set(null);
    const payload = this.buildPayload();
    this.http.post<PreviewResponse>('/api/programmes/preview', payload).subscribe({
      next: (response) => {
        this.previewing.set(false);
        if (JSON.stringify(payload) !== JSON.stringify(this.buildPayload())) {
          this.status.set('Settings changed. Generate a fresh preview.');
          return;
        }
        this.preview.set(response.preview);
        this.violations.set(response.preview.blocking_violations);
        this.status.set('Preview generated from the current Hevy source revisions.');
      },
      error: (err: unknown) => {
        this.previewing.set(false);
        this.error.set(this.describeError(err, 'Could not generate the programme preview.'));
      },
    });
  }

  activatePreview(): void {
    const preview = this.preview();
    if (
      !preview ||
      (preview.blocking_violations && preview.blocking_violations.length > 0) ||
      this.activating() ||
      this.previewing() ||
      this.refreshing()
    ) {
      return;
    }
    this.error.set(null);
    this.status.set(null);
    this.violations.set([]);
    this.conflict.set(null);
    this.remediation.set([]);
    this.activating.set(true);
    const activation: ProgrammeActivationPayload = {
      ...this.buildPayload(),
      preview_token: preview.preview_token,
      source_hashes: (preview.source_snapshots ?? []).map((snapshot) => ({
        routine_id: snapshot.id,
        source_hash: snapshot.source_hash,
      })),
    };
    this.http
      .post<{ status: string }>('/api/programmes/activate', activation)
      .subscribe({
        next: () => {
          this.activating.set(false);
          this.status.set('Your Hevy-derived programme is now active.');
          this.preview.set(null);
          this.loadProgrammes(false, true);
        },
        error: (err: unknown) => {
          this.activating.set(false);
          if (err instanceof HttpErrorResponse && err.status === 409) {
            const detail = this.programmeErrorDetail(err);
            this.conflict.set(detail?.conflict ?? null);
            this.remediation.set(detail?.remediation ?? []);
            this.error.set(detail?.message ?? 'The Hevy source changed after the preview.');
            return;
          }
          if (err instanceof HttpErrorResponse && err.status === 422) {
            const detail = this.programmeErrorDetail(err);
            if (detail?.violations?.length) {
              this.violations.set(detail.violations);
              this.error.set(detail?.message ?? 'The programme violates blocking constraints.');
              return;
            }
          }
          this.error.set(this.describeError(err, 'Could not activate the programme.'));
        },
      });
  }

  private errorBody(err: unknown): { detail?: unknown; message?: unknown } | null {
    if (typeof err !== 'object' || err === null) {
      return null;
    }
    const body = (err as { error?: unknown }).error;
    if (typeof body !== 'object' || body === null) {
      return null;
    }
    return body as { detail?: unknown; message?: unknown };
  }

  private programmeErrorDetail(err: unknown): ProgrammeErrorDetail | null {
    const detail = this.errorBody(err)?.detail;
    if (typeof detail !== 'object' || detail === null) {
      return null;
    }
    return detail as ProgrammeErrorDetail;
  }

  private describeError(err: unknown, fallback: string): string {
    const body = this.errorBody(err);
    const detail = body?.detail;
    if (typeof detail === 'string' && detail.length) {
      return detail;
    }
    const programmeDetail = this.programmeErrorDetail(err);
    if (programmeDetail?.message) {
      return programmeDetail.message;
    }
    if (typeof body?.message === 'string' && body.message.length) {
      return body.message;
    }
    return fallback;
  }
}
