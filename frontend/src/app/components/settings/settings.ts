import { HlmCardImports } from '@spartan-ng/ui/card';
import { HlmButtonImports } from '@spartan-ng/ui/button';
import { HlmBadgeImports } from '@spartan-ng/ui/badge';
import { HlmInputImports } from '@spartan-ng/ui/input';
import { HlmLabelImports } from '@spartan-ng/ui/label';
import { HlmAlertImports } from '@spartan-ng/ui/alert';
import { Component, inject, signal, OnInit } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { FormsModule } from '@angular/forms';

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [CommonModule, FormsModule, HlmCardImports, HlmButtonImports, HlmBadgeImports, HlmInputImports, HlmLabelImports, HlmAlertImports],
  templateUrl: './settings.html',
  styleUrl: './settings.css'
})
export class Settings implements OnInit {
  saveKey(p: any) {}
  verifyHevy() {}
  syncHistory() {}
  deleteKey(p: any) {}
  selectProvider(p: any) {}
  toggleChip(p: any) {}
  selectSingle(p1: any, p2: any) {}
  savePreferences() {}

  private http = inject(HttpClient);
  protected sanitizer = inject(DomSanitizer);
  
  data = signal<any>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);

  ngOnInit() {
    this.http.get('/api/settings').subscribe({
      next: (data: any) => {
        this.data.set(data);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set('Failed to load settings data');
        this.loading.set(false);
      }
    });
  }
  
  safeHtml(html: string): SafeHtml {
    if (!html) return '';
    return this.sanitizer.bypassSecurityTrustHtml(html);
  }
}
