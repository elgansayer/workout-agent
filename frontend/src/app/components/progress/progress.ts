import { HlmCardImports } from '@spartan-ng/ui/card';
import { HlmButtonImports } from '@spartan-ng/ui/button';
import { HlmBadgeImports } from '@spartan-ng/ui/badge';
import { HlmAlertImports } from '@spartan-ng/ui/alert';
import { Component, inject, signal, OnInit } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { FormsModule } from '@angular/forms';

@Component({
  selector: 'app-progress',
  standalone: true,
  imports: [CommonModule, FormsModule, HlmCardImports, HlmButtonImports, HlmBadgeImports, HlmAlertImports],
  templateUrl: './progress.html',
  styleUrl: './progress.css'
})
export class Progress implements OnInit {
  toggleXAI(name: string) {}
  fetchXAI(chart: any) { this.toggleXAI(chart.name); }
  isXAIExpanded: any = {};

  xaiLoading: any = {};
  xaiResult: any = {};

  private http = inject(HttpClient);
  protected sanitizer = inject(DomSanitizer);
  
  data = signal<any>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);

  ngOnInit() {
    this.http.get('/api/progress').subscribe({
      next: (data: any) => {
        this.data.set(data);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set('Failed to load progress data');
        this.loading.set(false);
      }
    });
  }
  
  safeHtml(html: string): SafeHtml {
    if (!html) return '';
    return this.sanitizer.bypassSecurityTrustHtml(html);
  }
}
