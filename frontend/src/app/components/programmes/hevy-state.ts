import { Component, input } from '@angular/core';
import type { BuilderData } from './programme-contracts';

/** Step 1 of the builder: the authenticated user's Hevy connection state. */
@Component({
  selector: 'app-hevy-state',
  standalone: true,
  templateUrl: './hevy-state.html',
  styleUrl: './hevy-state.css',
})
export class HevyState {
  readonly data = input<BuilderData | null>(null);
}
