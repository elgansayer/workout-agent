import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { App } from './app';
import { Auth } from './services/auth';
import { DEFAULT_DISPLAY_SETTINGS, DisplaySettingsService } from './services/display-settings';

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
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: Auth, useValue: authStub },
      ],
    }).compileComponents();
  });

  it("loads the signed-in user's display settings and applies them to the page", async () => {
    const fixture = TestBed.createComponent(App);
    TestBed.inject(HttpTestingController)
      .expectOne('/api/settings/display')
      .flush({
        settings: {
          ...DEFAULT_DISPLAY_SETTINGS,
          locale: 'en-US',
          motion: 'reduce',
          contrast: 'more',
          text_size: 'large',
        },
        options: null,
      });
    await fixture.whenStable();

    const root = document.documentElement;
    expect(TestBed.inject(DisplaySettingsService).settings().locale).toBe('en-US');
    expect(root.lang).toBe('en-US');
    expect(root.dataset['motion']).toBe('reduce');
    expect(root.dataset['contrast']).toBe('more');
    expect(root.dataset['textSize']).toBe('large');
    root.removeAttribute('lang');
    delete root.dataset['motion'];
    delete root.dataset['contrast'];
    delete root.dataset['textSize'];
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
