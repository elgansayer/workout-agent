import { Injectable, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

export interface User {
  id: string;
  email: string;
  name?: string;
  picture?: string;
}

@Injectable({ providedIn: 'root' })
export class Auth {
  private http = inject(HttpClient);
  
  // Signal holding the current user state
  currentUser = signal<User | null | undefined>(undefined); // undefined means loading

  async checkAuth() {
    try {
      const response = await firstValueFrom(this.http.get<{user: User}>('/api/me'));
      this.currentUser.set(response.user);
    } catch (e) {
      this.currentUser.set(null);
    }
  }

  login() {
    window.location.href = '/login/google';
  }

  logout() {
    window.location.href = '/logout';
  }
}
