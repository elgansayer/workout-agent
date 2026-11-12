import { PagedList } from '../../shared/paged-list';
import { ListControls } from '../../shared/list-controls';
import { ProvenanceBadge } from '../../shared/provenance-badge';
import { BodyReading, Provenance } from '../../shared/provenance';
import { Component, inject, signal, computed, OnInit } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { DisplaySettingsService } from '../../services/display-settings';
import { LocalDatePipe } from '../../shared/local-format.pipes';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { SetupNotice } from '../../shared/setup-notice';
import {
  SetupState,
  ViewState,
  needsAttention,
  resolveViewState,
} from '../../shared/setup-state';

export const CORRECTABLE_FIELDS = [
  { key: 'weight_kg', label: 'Weight (kg)', unit: 'kg' },
  { key: 'body_fat_pct', label: 'Body fat (%)', unit: '% body fat' },
  { key: 'muscle_pct', label: 'Muscle (%)', unit: '% muscle' },
  { key: 'resting_hr', label: 'Resting HR (bpm)', unit: 'bpm' },
] as const;

type CorrectableKey = (typeof CORRECTABLE_FIELDS)[number]['key'];

interface CorrectionDraft {
  values: Record<CorrectableKey, string>;
  reason: string;
}

const emptyDraft = (): CorrectionDraft => ({
  values: { weight_kg: '', body_fat_pct: '', muscle_pct: '', resting_hr: '' },
  reason: '',
});

@Component({
  selector: 'app-progress',
  standalone: true,
  imports: [ListControls, ProvenanceBadge, CommonModule, FormsModule, SetupNotice, LocalDatePipe],
  templateUrl: './progress.html',
  styleUrl: './progress.css',
})
export class Progress implements OnInit {
  private http = inject(HttpClient);
  private sanitizer = inject(DomSanitizer);
  private readonly display = inject(DisplaySettingsService);

  data = signal<any>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);
  xaiReasoning = signal<{ [key: string]: string }>({});
  xaiProvenance = signal<{ [key: string]: Provenance | null }>({});
  setup = signal<SetupState | null>(null);

  readonly viewState = computed<ViewState>(() =>
    resolveViewState({
      loading: this.loading(),
      error: this.error(),
      hasData: this.hasData(),
      attention: needsAttention(this.setup()),
    }),
  );

  readonly hasData = computed(() => {
    const data = this.data();
    if (!data) return false;
    if ((data.charts?.length ?? 0) > 0) return true;
    if ((data.body_readings?.length ?? 0) > 0) return true;
    const body = data.body ?? {};
    return !!(body.weight || body.body_fat || body.muscle || body.resting_hr);
  });

  readonly fields = CORRECTABLE_FIELDS;
  editingDate = signal<string | null>(null);
  draft: CorrectionDraft = emptyDraft();
  saving = signal<boolean>(false);
  correctionError = signal<string | null>(null);
  correctionNotice = signal<string | null>(null);
  lineage = signal<{ [date: string]: Provenance[] }>({});

  readonly lifts = new PagedList<any>(
    () => this.data()?.charts ?? [],
    (item) => item.name,
    6,
  );

  ngOnInit() {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.error.set(null);
    this.http.get('/api/progress').subscribe({
      next: (data: any) => {
        this.data.set(data);
        this.setup.set(data?.setup ?? null);
        this.loading.set(false);
      },
      error: () => {
        this.error.set('Failed to load progress data');
        this.loading.set(false);
      },
    });
  }

  private readonly explanationRequests = new Map<string, number>();

  toggleXAI(exerciseName: string) {
    const request = (this.explanationRequests.get(exerciseName) ?? 0) + 1;
    this.explanationRequests.set(exerciseName, request);
    const current = this.xaiReasoning();
    if (current[exerciseName]) {
      const next = { ...current };
      delete next[exerciseName];
      this.xaiReasoning.set(next);
      return;
    }

    this.xaiReasoning.set({ ...current, [exerciseName]: 'Analyzing history...' });
    const today = this.display.localToday();
    const contextId = `${today}_${exerciseName}`;

    this.http.get(`/api/xai_reasoning/${encodeURIComponent(contextId)}`).subscribe({
      next: (res: any) => {
        if (this.explanationRequests.get(exerciseName) !== request) return;
        this.xaiReasoning.set({
          ...this.xaiReasoning(),
          [exerciseName]: res.reasoning || 'No explanation available.',
        });
        this.xaiProvenance.set({
          ...this.xaiProvenance(),
          [exerciseName]: res.provenance ?? null,
        });
      },
      error: () => {
        if (this.explanationRequests.get(exerciseName) !== request) return;
        this.xaiReasoning.set({
          ...this.xaiReasoning(),
          [exerciseName]: 'Error analyzing history.',
        });
      },
    });
  }

  readingSummary(reading: BodyReading): string {
    const parts = CORRECTABLE_FIELDS.filter((f) => reading[f.key] !== null).map(
      (f) => `${reading[f.key]} ${f.unit}`,
    );
    return parts.length ? parts.join(' · ') : 'No values';
  }

  revisionSummary(revision: Provenance): string {
    const values = revision.content?.values ?? {};
    const parts = CORRECTABLE_FIELDS.filter((f) => values[f.key] !== undefined).map(
      (f) => `${values[f.key]} ${f.unit}`,
    );
    return parts.join(' · ');
  }

  startCorrection(reading: BodyReading): void {
    const draft = emptyDraft();
    for (const field of CORRECTABLE_FIELDS) {
      const value = reading[field.key];
      draft.values[field.key] = value === null ? '' : String(value);
    }
    this.draft = draft;
    this.editingDate.set(reading.date);
    this.correctionError.set(null);
    this.correctionNotice.set(null);
  }

  cancelCorrection(): void {
    this.editingDate.set(null);
    this.correctionError.set(null);
  }

  submitCorrection(reading: BodyReading): void {
    const changes: { [key: string]: number } = {};
    for (const field of CORRECTABLE_FIELDS) {
      const raw = this.draft.values[field.key].trim();
      if (raw === '') continue;
      const value = Number(raw);
      if (!Number.isFinite(value)) {
        this.correctionError.set(`${field.label} must be a number.`);
        return;
      }
      if (value !== reading[field.key]) changes[field.key] = value;
    }
    if (Object.keys(changes).length === 0) {
      this.correctionError.set('Change at least one value to record a correction.');
      return;
    }
    const reason = this.draft.reason.trim();
    this.saving.set(true);
    this.correctionError.set(null);
    this.http
      .post<{ lineage: Provenance[] }>('/api/body-metrics/corrections', {
        date: reading.date,
        ...changes,
        ...(reason ? { reason } : {}),
      })
      .subscribe({
        next: (result) => {
          this.saving.set(false);
          this.editingDate.set(null);
          this.correctionNotice.set(
            `Saved your correction for ${reading.date}. The original reading is kept in its history.`,
          );
          this.lineage.set({ ...this.lineage(), [reading.date]: result.lineage });
          this.load();
        },
        error: (err: HttpErrorResponse) => {
          this.saving.set(false);
          this.correctionError.set(this.errorDetail(err));
        },
      });
  }

  toggleLineage(date: string): void {
    const current = this.lineage();
    if (current[date]) {
      const next = { ...current };
      delete next[date];
      this.lineage.set(next);
      return;
    }
    this.http
      .get<{ revisions: Provenance[] }>(`/api/provenance/body_metrics/${encodeURIComponent(date)}`)
      .subscribe({
        next: (result) => this.lineage.set({ ...this.lineage(), [date]: result.revisions }),
        error: () => this.lineage.set({ ...this.lineage(), [date]: [] }),
      });
  }

  private errorDetail(err: HttpErrorResponse): string {
    const detail = err.error?.detail;
    if (typeof detail === 'string' && detail) return detail;
    if (Array.isArray(detail)) {
      const messages = detail.map((item) => item?.msg).filter(Boolean);
      if (messages.length) return messages.join(' ');
    }
    return 'Could not save the correction. Try again.';
  }

  safeHtml(html: string): SafeHtml {
    return this.sanitizer.bypassSecurityTrustHtml(html || '');
  }
}
