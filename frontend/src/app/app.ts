import { Component, ElementRef, HostListener, ViewChild, inject, signal } from '@angular/core';
import { NavigationEnd, Router, RouterOutlet, RouterLink, RouterLinkActive } from '@angular/router';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Auth } from './services/auth';
import { SetupStateService } from './services/setup-state.service';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App {
  protected auth = inject(Auth);
  protected setup = inject(SetupStateService);
  protected menuOpen = signal(false);
  @ViewChild('menuButton') private menuButton?: ElementRef<HTMLButtonElement>;
  @ViewChild('mainContent') private mainContent?: ElementRef<HTMLElement>;

  constructor() {
    this.auth.checkAuth();
    this.setup.load();
    inject(Router)
      .events.pipe(takeUntilDestroyed())
      .subscribe((event) => {
        if (event instanceof NavigationEnd) {
          this.menuOpen.set(false);
          this.mainContent?.nativeElement.focus({ preventScroll: true });
          this.setup.load();
        }
      });
  }

  @HostListener('document:keydown.escape')
  closeMenu(): void {
    if (this.menuOpen()) {
      this.menuOpen.set(false);
      this.menuButton?.nativeElement.focus();
    }
  }
}
