import { HlmCardImports } from '@spartan-ng/ui/card';
import { HlmAlertImports } from '@spartan-ng/ui/alert';
import { Component, inject, signal, OnInit } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { FormsModule } from '@angular/forms';

@Component({
  selector: 'app-checkins',
  standalone: true,
  imports: [CommonModule, FormsModule, HlmCardImports, HlmAlertImports],
  templateUrl: './checkins.html',
  styleUrl: './checkins.css'
})
export class Checkins implements OnInit {
  private http = inject(HttpClient);
  protected sanitizer = inject(DomSanitizer);
  
  data = signal<any>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);

  ngOnInit() {
    this.http.get('/api/checkins').subscribe({
      next: (data: any) => {
        this.data.set(data);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set('Failed to load checkins data');
        this.loading.set(false);
      }
    });
  }
  
  safeHtml(html: string): SafeHtml {
    if (!html) return '';
    return this.sanitizer.bypassSecurityTrustHtml(html);
  }
}
