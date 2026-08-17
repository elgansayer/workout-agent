import { Component, inject, signal, OnInit } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { FormsModule } from '@angular/forms';

@Component({
  selector: 'app-chat',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './chat.html',
  styleUrl: './chat.css'
})
export class Chat implements OnInit {
  private http = inject(HttpClient);
  protected sanitizer = inject(DomSanitizer);
  
  data = signal<any>(null);
  loading = signal<boolean>(true);
  error = signal<string | null>(null);

  ngOnInit() {
    this.http.get('/api/chat').subscribe({
      next: (data: any) => {
        this.data.set(data);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set('Failed to load chat data');
        this.loading.set(false);
      }
    });
  }
  
  safeHtml(html: string): SafeHtml {
    if (!html) return '';
    return this.sanitizer.bypassSecurityTrustHtml(html);
  }
}
