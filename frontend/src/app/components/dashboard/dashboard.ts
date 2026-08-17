import { Component, inject, signal, OnInit } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './dashboard.html',
  styleUrl: './dashboard.css'
})
export class Dashboard implements OnInit {
  private http = inject(HttpClient);
  private sanitizer = inject(DomSanitizer);
  
  data = signal<any>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);

  cycleRing = signal<SafeHtml | null>(null);
  blockRing = signal<SafeHtml | null>(null);
  calendar = signal<SafeHtml | null>(null);

  ngOnInit() {
    this.http.get('/api/dashboard').subscribe({
      next: (data: any) => {
        this.data.set(data);
        if (data.cycle_ring) this.cycleRing.set(this.sanitizer.bypassSecurityTrustHtml(data.cycle_ring));
        if (data.block_ring) this.blockRing.set(this.sanitizer.bypassSecurityTrustHtml(data.block_ring));
        if (data.calendar) this.calendar.set(this.sanitizer.bypassSecurityTrustHtml(data.calendar));
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set('Failed to load dashboard data');
        this.loading.set(false);
      }
    });
  }
}
