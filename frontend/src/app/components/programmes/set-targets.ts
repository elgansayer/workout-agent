import { Component, input } from '@angular/core';
import type { HevySetSummary } from './programme-contracts';

/** Renders the exact provider set targets of one Hevy routine exercise. */
@Component({
  selector: 'app-set-targets',
  standalone: true,
  template: `
    @for (target of targets(); track target.index) {
      <code>
        Set {{ target.index }} · {{ target.type }}
        @if (target.load_kg !== null && target.load_kg !== undefined) {
          · {{ target.load_kg }} kg
        }
        @if (target.reps !== null && target.reps !== undefined) {
          · {{ target.reps }} reps
        }
        @if (target.rep_range) {
          · {{ target.rep_range.start }}–{{ target.rep_range.end }} reps
        }
        @if (target.distance_meters !== null && target.distance_meters !== undefined) {
          · {{ target.distance_meters }} m
        }
        @if (target.duration_seconds !== null && target.duration_seconds !== undefined) {
          · {{ target.duration_seconds }} s
        }
        @if (target.rpe !== null && target.rpe !== undefined) {
          · RPE {{ target.rpe }}
        }
      </code>
    } @empty {
      <code>No explicit Hevy set target</code>
    }
  `,
})
export class SetTargets {
  readonly targets = input<HevySetSummary[]>([]);
}
