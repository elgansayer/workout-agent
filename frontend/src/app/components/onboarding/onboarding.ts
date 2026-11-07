import { Component, inject, OnInit, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';

@Component({
  selector: 'app-onboarding',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './onboarding.html',
  styleUrl: './onboarding.css',
})
export class Onboarding implements OnInit {
  private http = inject(HttpClient);

  data = signal<any>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);
  skipError = signal<string | null>(null);
  busyStep = signal<string | null>(null);

  ngOnInit() {
    this.refresh();
  }

  refresh() {
    this.loading.set(true);
    this.error.set(null);
    this.http.get('/api/onboarding').subscribe({
      next: (data: any) => {
        this.data.set(data);
        this.loading.set(false);
      },
      error: () => {
        this.error.set('Failed to load setup progress');
        this.loading.set(false);
      },
    });
  }

  skip(step: string) {
    if (this.busyStep()) return;
    this.busyStep.set(step);
    this.skipError.set(null);
    this.http.post('/api/onboarding/skip', { step }).subscribe({
      next: (data: any) => {
        this.data.set(data);
        this.busyStep.set(null);
      },
      error: (err: any) => {
        this.skipError.set(err.error?.detail || 'Could not skip this step.');
        this.busyStep.set(null);
      },
    });
  }

  statusLabel(step: any): string {
    if (step.status === 'complete') return 'Done';
    if (step.status === 'skipped') return 'Skipped';
    return step.required ? 'Required' : 'Optional';
  }
}
