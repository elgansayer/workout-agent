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
  selector: 'app-programmes',
  standalone: true,
  imports: [CommonModule, FormsModule, HlmCardImports, HlmButtonImports, HlmBadgeImports, HlmAlertImports],
  templateUrl: './programmes.html',
  styleUrl: './programmes.css'
})
export class Programmes implements OnInit {
  private http = inject(HttpClient);
  protected sanitizer = inject(DomSanitizer);

  data = signal<any>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);
  selectingKey = signal<string | null>(null);

  ngOnInit() {
    this.loadProgrammes();
  }

  loadProgrammes() {
    this.http.get('/api/programmes').subscribe({
      next: (data: any) => {
        this.data.set(data);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set('Failed to load programmes data');
        this.loading.set(false);
      }
    });
  }

  selectProgramme(templateKey: string) {
    if (this.selectingKey()) return;
    this.error.set(null);
    this.selectingKey.set(templateKey);
    this.http.post('/api/programmes/select', { template_key: templateKey }).subscribe({
      next: () => {
        this.selectingKey.set(null);
        this.loadProgrammes();
      },
      error: (err: any) => {
        this.selectingKey.set(null);
        this.error.set(err.error?.detail || 'Failed to select programme template');
      }
    });
  }

  safeHtml(html: string): SafeHtml {
    if (!html) return '';
    return this.sanitizer.bypassSecurityTrustHtml(html);
  }
}
