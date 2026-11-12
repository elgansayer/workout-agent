import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Onboarding } from './onboarding';
import { OnboardingState } from '../../services/onboarding.service';

function state(overrides: Partial<OnboardingState> = {}): OnboardingState {
  return {
    steps: [
      {
        id: 'preferences',
        title: 'Set your account preferences',
        description: 'Choose goals.',
        href: '/settings?section=profile',
        optional: true,
        status: 'pending',
        skip_consequence: 'Defaults will be used.',
      },
      {
        id: 'hevy',
        title: 'Connect Hevy',
        description: 'Import routines.',
        href: '/settings?section=connections',
        optional: false,
        status: 'pending',
        skip_consequence: null,
      },
      {
        id: 'health_data',
        title: 'Connect health data',
        description: 'Import sleep.',
        href: '/settings?section=connections',
        optional: true,
        status: 'pending',
        skip_consequence: 'Only workouts will be used.',
      },
    ],
    next_step: {
      id: 'hevy',
      title: 'Connect Hevy',
      description: 'Import routines.',
      href: '/settings?section=connections',
      optional: false,
      status: 'pending',
      skip_consequence: null,
    },
    account_ready: false,
    completed_count: 0,
    skipped_count: 0,
    total_steps: 3,
    progress_percent: 0,
    ...overrides,
  };
}

async function create() {
  await TestBed.configureTestingModule({
    imports: [Onboarding],
    providers: [provideHttpClient(), provideHttpClientTesting()],
  }).compileComponents();
  const http = TestBed.inject(HttpTestingController);
  const fixture = TestBed.createComponent(Onboarding);
  fixture.detectChanges();
  http.expectOne('/api/onboarding').flush(state());
  await fixture.whenStable();
  fixture.detectChanges();
  return { fixture, http, el: fixture.nativeElement as HTMLElement };
}

describe('Onboarding', () => {
  afterEach(() => TestBed.inject(HttpTestingController).verify());

  it('renders the verified sequence and explains skipped consequences', async () => {
    const { el } = await create();

    expect(el.textContent).toContain('Connect Hevy');
    expect(el.textContent).toContain('Connect health data');
    expect(el.textContent).toContain('Only workouts will be used.');
    expect(el.querySelector('a[href="/settings?section=connections"]')).toBeTruthy();
    expect(el.querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow')).toBe('0');
  });

  it('persists a skip for an optional step and updates the sequence', async () => {
    const { fixture, http, el } = await create();
    const healthItem = [...el.querySelectorAll<HTMLElement>('.onboarding-step')].find((item) =>
      item.textContent?.includes('Connect health data'),
    );
    const skipButton = [...(healthItem?.querySelectorAll<HTMLButtonElement>('button') ?? [])].find(
      (button) => button.textContent?.includes('Skip for now'),
    );
    expect(skipButton).toBeTruthy();

    skipButton?.click();
    const request = http.expectOne('/api/onboarding/skip');
    expect(request.request.body).toEqual({ step: 'health_data' });
    request.flush(
      state({
        steps: state().steps.map((step) =>
          step.id === 'health_data' ? { ...step, status: 'skipped' as const } : step,
        ),
        skipped_count: 1,
        progress_percent: 33,
      }),
    );
    await fixture.whenStable();
    fixture.detectChanges();

    expect(el.textContent).toContain('Skipped for now.');
    expect(el.textContent).toContain('Revisit this step');
  });

  it('resumes a previously skipped step', async () => {
    await TestBed.configureTestingModule({
      imports: [Onboarding],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    const http = TestBed.inject(HttpTestingController);
    const fixture = TestBed.createComponent(Onboarding);
    fixture.detectChanges();
    const skipped = state({
      steps: state().steps.map((step) =>
        step.id === 'health_data' ? { ...step, status: 'skipped' as const } : step,
      ),
      skipped_count: 1,
      progress_percent: 33,
    });
    http.expectOne('/api/onboarding').flush(skipped);
    await fixture.whenStable();
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;

    const healthItem = [...el.querySelectorAll<HTMLElement>('.onboarding-step')].find((item) =>
      item.textContent?.includes('Connect health data'),
    );
    const resumeButton = [
      ...(healthItem?.querySelectorAll<HTMLButtonElement>('button') ?? []),
    ].find((button) => button.textContent?.includes('Revisit this step'));
    resumeButton?.click();
    const request = http.expectOne('/api/onboarding/resume');
    expect(request.request.body).toEqual({ step: 'health_data' });
    request.flush(state({ progress_percent: 0 }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(el.textContent).not.toContain('Revisit this step');
  });
});
