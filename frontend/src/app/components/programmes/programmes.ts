import { PagedList } from '../../shared/paged-list';
import { ListControls } from '../../shared/list-controls';
import { ReadinessBanner } from '../../shared/readiness-banner';
import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { HlmAlertImports } from '@spartan-ng/ui/alert';
import { HlmBadgeImports } from '@spartan-ng/ui/badge';
import { HlmButtonImports } from '@spartan-ng/ui/button';
import { HlmCardImports } from '@spartan-ng/ui/card';

@Component({
  selector: 'app-programmes',
  standalone: true,
  imports: [
    CommonModule,
    ListControls,
    FormsModule,
    HlmCardImports,
    HlmButtonImports,
    HlmBadgeImports,
    HlmAlertImports,
    ReadinessBanner,
  ],
  templateUrl: './programmes.html',
  styleUrl: './programmes.css',
})
export class Programmes implements OnInit {
  private readonly http = inject(HttpClient);

  data = signal<any>(null);
  preview = signal<any>(null);
  loading = signal(true);
  refreshing = signal(false);
  previewing = signal(false);
  activating = signal(false);
  error = signal<string | null>(null);
  status = signal<string | null>(null);
  selectedRoutineIds = signal<string[]>([]);

  folderFilter = signal('');
  readonly routineList = new PagedList<any>(
    () =>
      (this.data()?.source?.routines ?? []).filter(
        (routine: any) =>
          !this.folderFilter() || String(routine.folder_id ?? '') === this.folderFilter(),
      ),
    (routine) =>
      [
        routine.title,
        routine.folder_name,
        ...(routine.muscle_summary ?? []),
        ...(routine.exercises ?? []).map((exercise: any) => exercise.title),
      ].join(' '),
    6,
  );
  durationWeeks = 12;
  goal = 'general_fitness';
  startDate = new Date().toISOString().slice(0, 10);
  sessionsPerWeek: number | null = null;
  experience = 'intermediate';
  maxSessionMinutes: number | null = null;
  adaptationAggressiveness = 'balanced';

  ngOnInit(): void {
    this.loadProgrammes();
  }

  loadProgrammes(refresh = false, preserveStatus = false): void {
    this.error.set(null);
    if (!preserveStatus) this.status.set(null);
    this.preview.set(null);
    if (refresh) {
      this.refreshing.set(true);
    } else {
      this.loading.set(true);
    }

    this.http.get('/api/programmes').subscribe({
      next: (data: any) => {
        this.data.set(data);
        const available = new Set((data.source?.routines ?? []).map((routine: any) => routine.id));
        const activeIds: string[] =
          data.active_programme?.definition?.programme_spec?.selected_routine_ids ?? [];
        const current = this.selectedRoutineIds().filter((id) => available.has(id));
        if (!current.length && activeIds.length) {
          this.selectedRoutineIds.set(activeIds.filter((id) => available.has(id)));
        } else if (current.length !== this.selectedRoutineIds().length) {
          this.selectedRoutineIds.set(current);
        }
        this.loading.set(false);
        this.refreshing.set(false);
      },
      error: (err: any) => {
        this.error.set(err.error?.detail || 'Failed to load your Hevy routine library.');
        this.loading.set(false);
        this.refreshing.set(false);
      },
    });
  }

  visibleRoutines(): any[] {
    return this.routineList.visible();
  }

  invalidatePreview(): void {
    this.preview.set(null);
  }

  filterFolder(folder: string): void {
    this.folderFilter.set(folder);
    this.routineList.reset();
  }

  isSelected(routineId: string): boolean {
    return this.selectedRoutineIds().includes(routineId);
  }

  toggleRoutine(routineId: string): void {
    this.preview.set(null);
    const selected = this.selectedRoutineIds();
    this.selectedRoutineIds.set(
      selected.includes(routineId)
        ? selected.filter((id) => id !== routineId)
        : [...selected, routineId],
    );
  }

  routineById(routineId: string): any | null {
    return (
      (this.data()?.source?.routines ?? []).find((routine: any) => routine.id === routineId) ?? null
    );
  }

  moveRoutine(index: number, delta: number): void {
    const nextIndex = index + delta;
    const selected = [...this.selectedRoutineIds()];
    if (nextIndex < 0 || nextIndex >= selected.length) {
      return;
    }
    [selected[index], selected[nextIndex]] = [selected[nextIndex], selected[index]];
    this.selectedRoutineIds.set(selected);
    this.preview.set(null);
  }

  removeRoutine(routineId: string): void {
    this.selectedRoutineIds.set(this.selectedRoutineIds().filter((id) => id !== routineId));
    this.preview.set(null);
  }

  private buildPayload(): any {
    return {
      selected_routine_ids: this.selectedRoutineIds(),
      duration_weeks: Number(this.durationWeeks),
      goal: this.goal,
      start_date: this.startDate,
      sessions_per_week: this.sessionsPerWeek === null ? null : Number(this.sessionsPerWeek),
      experience: this.experience,
      max_session_minutes: this.maxSessionMinutes === null ? null : Number(this.maxSessionMinutes),
      adaptation_aggressiveness: this.adaptationAggressiveness,
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
    this.previewing.set(true);
    this.preview.set(null);
    const payload = this.buildPayload();
    this.http.post<any>('/api/programmes/preview', payload).subscribe({
      next: (response) => {
        this.previewing.set(false);
        if (JSON.stringify(payload) !== JSON.stringify(this.buildPayload())) {
          this.status.set('Settings changed. Generate a fresh preview.');
          return;
        }
        this.preview.set(response.preview);
        this.status.set('Preview generated from the current Hevy source revisions.');
      },
      error: (err: any) => {
        this.previewing.set(false);
        this.error.set(err.error?.detail || 'Could not generate the programme preview.');
      },
    });
  }

  activatePreview(): void {
    const preview = this.preview();
    if (!preview || this.activating() || this.previewing() || this.refreshing()) {
      return;
    }
    this.error.set(null);
    this.status.set(null);
    this.activating.set(true);
    this.http
      .post<any>('/api/programmes/activate', {
        ...this.buildPayload(),
        preview_token: preview.preview_token,
      })
      .subscribe({
        next: () => {
          this.activating.set(false);
          this.status.set('Your Hevy-derived programme is now active.');
          this.preview.set(null);
          this.loadProgrammes(false, true);
        },
        error: (err: any) => {
          this.activating.set(false);
          this.error.set(err.error?.detail || 'Could not activate the programme.');
        },
      });
  }
}
