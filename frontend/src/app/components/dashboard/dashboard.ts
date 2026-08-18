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

  ngOnInit() {
    this.http.get('/api/dashboard').subscribe({
      next: (data: any) => {
        this.data.set(data);
        this.loading.set(false);
      },
      error: () => {
        this.error.set('Failed to load dashboard data');
        this.loading.set(false);
      }
    });
  }

  safeHtml(html: string): SafeHtml {
    return this.sanitizer.bypassSecurityTrustHtml(html || '');
  }
}
