import { CommonModule } from '@angular/common';
import { Component, input, output } from '@angular/core';
import type {
  HevyRoutine,
  ProgrammePreview as ProgrammePreviewData,
  ProgrammeViolation,
  SourceConflict,
} from './programme-contracts';
import { SetTargets } from './set-targets';

/**
 * Shows the selected Hevy source routines and the generated programme preview.
 *
 * The component is read-only apart from the activation request it emits.
 */
@Component({
  selector: 'app-programme-preview',
  standalone: true,
  imports: [CommonModule, SetTargets],
  templateUrl: './programme-preview.html',
  styleUrl: './programme-preview.css',
})
export class ProgrammePreview {
  readonly preview = input<ProgrammePreviewData | null>(null);
  readonly routines = input<HevyRoutine[]>([]);
  readonly violations = input<ProgrammeViolation[]>([]);
  readonly conflict = input<SourceConflict | null>(null);
  readonly remediation = input<string[]>([]);
  readonly activating = input(false);
  readonly previewing = input(false);
  readonly activate = output<void>();
  readonly refresh = output<void>();
}
