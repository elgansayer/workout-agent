import { PagedList } from '../../shared/paged-list';
import { ListControls } from '../../shared/list-controls';
import { HlmCardImports } from '@spartan-ng/ui/card';
import { HlmAlertImports } from '@spartan-ng/ui/alert';
import { Component, inject, signal, OnInit } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { FormsModule } from '@angular/forms';

@Component({
  selector: 'app-stats',
  standalone: true,
  imports: [ListControls, CommonModule, FormsModule, HlmCardImports, HlmAlertImports],
  templateUrl: './stats.html',
  styleUrl: './stats.css',
})
export class Stats implements OnInit {
  private http = inject(HttpClient);
  protected sanitizer = inject(DomSanitizer);

  data = signal<any>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);

  readonly records = new PagedList<any>(
    () => this.data()?.prs ?? [],
    (item) => item.exercise,
    10,
  );

  ngOnInit() {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.error.set(null);
    this.http.get('/api/stats').subscribe({
      next: (data: any) => {
        this.data.set(data);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set('Failed to load stats data');
        this.loading.set(false);
      },
    });
  }

  safeHtml(html: string): SafeHtml {
    if (!html) return '';
    return this.sanitizer.bypassSecurityTrustHtml(html);
  }
}
