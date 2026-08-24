import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { App } from './app';
import { Auth } from './services/auth';

describe('App', () => {
  const authStub = {
    currentUser: signal(null),
    checkAuth: () => undefined,
    logout: () => undefined,
  };

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [App],
      providers: [
        provideRouter([]),
        { provide: Auth, useValue: authStub },
      ],
    }).compileComponents();
  });

  it('should create the app', () => {
    const fixture = TestBed.createComponent(App);
    expect(fixture.componentInstance).toBeTruthy();
  });

  it('should render an accessible primary navigation and skip link', () => {
    const fixture = TestBed.createComponent(App);
    fixture.detectChanges();

    const compiled = fixture.nativeElement as HTMLElement;
    const navigation = compiled.querySelector('nav[aria-label="Primary navigation"]');
    const skipLink = compiled.querySelector<HTMLAnchorElement>('a.skip-to-content');

    expect(navigation).toBeTruthy();
    expect(navigation?.querySelectorAll('a').length).toBeGreaterThanOrEqual(9);
    expect(skipLink?.getAttribute('href')).toBe('#main-content');
    expect(compiled.querySelector('main')?.id).toBe('main-content');
  });
});
