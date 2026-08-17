import { HlmCardImports } from '@spartan-ng/ui/card';
import { HlmButtonImports } from '@spartan-ng/ui/button';
import { HlmAlertImports } from '@spartan-ng/ui/alert';
import { Component, inject, signal } from '@angular/core';
import { Auth } from '../../services/auth';

@Component({
  selector: 'app-login',
  imports: [HlmCardImports, HlmButtonImports, HlmAlertImports],
  templateUrl: './login.html',
  styleUrl: './login.css'
})
export class Login {
  loading = signal<boolean>(false);
  error = signal<string|null>(null);
  data = signal<any>({});

  protected auth = inject(Auth);
}
