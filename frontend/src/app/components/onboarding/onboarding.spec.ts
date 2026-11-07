import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Onboarding } from './onboarding';

function step(overrides: Record<string, unknown>) {
  return {
    id: 'account',
    title: 'Account preferences',
    description: 'Choose goals.',
    required: true,
    action_label: 'Set preferences',
    href: '/settings',
    consequence: '',
    status: 'pending',
    completed: false,
    skipped: false,
    resolved: false,
    ...overrides,
  };
}

const pendingState = {
  steps: [
    step({ id: 'account', title: 'Account preferences', status: 'complete', completed: true, resolved: true }),
    step({ id: 'hevy', title: 'Connect Hevy', action_label: 'Connect Hevy', href: '/settings' }),
    step({
      id: 'health',
      title: 'Add health data',
      required: false,
      action_label: 'Connect health data',
      consequence: 'Without health data, recovery guidance stays limited.',
    }),
    step({ id: 'programme', title: 'Select your programme', href: '/programmes' }),
  ],
  ready: false,
  complete: false,
  next_action: { id: 'hevy', title: 'Connect Hevy' },
  next_required_action: {
    id: 'hevy',
    title: 'Connect Hevy',
    description: 'Connect your Hevy account so workouts sync.',
    action_label: 'Connect Hevy',
    href: '/settings',
  },
  total_count: 4,
  resolved_count: 1,
  completed_count: 1,
  required_total: 3,
  required_completed: 1,
  progress_percent: 25,
};

const readyState = {
  ...pendingState,
  steps: pendingState.steps.map((item) => ({
    ...item,
    status: item.required ? 'complete' : 'skipped',
    completed: item.required,
    skipped: !item.required,
    resolved: true,
  })),
  ready: true,
  complete: true,
  next_action: null,
  next_required_action: null,
  resolved_count: 4,
  completed_count: 3,
  required_completed: 3,
  progress_percent: 100,
};

describe('Onboarding', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Onboarding],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function load(state: any) {
    const fixture = TestBed.createComponent(Onboarding);
    fixture.detectChanges();
    http.expectOne('/api/onboarding').flush(state);
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  it('shows the next required action and explains optional skips', async () => {
    const fixture = await load(pendingState);
    const element = fixture.nativeElement as HTMLElement;

    expect(element.querySelector('.next-action')?.textContent).toContain('Connect Hevy');
    expect(element.textContent).toContain('Without health data, recovery guidance stays limited.');
    expect(element.querySelectorAll('button').length).toBeGreaterThan(0);
  });

  it('persists a skip and refreshes progress from the server response', async () => {
    const fixture = await load(pendingState);
    const element = fixture.nativeElement as HTMLElement;
    const skipButton = Array.from(element.querySelectorAll('button')).find((button) =>
      button.textContent?.includes('Skip for now'),
    );
    expect(skipButton).toBeTruthy();

    skipButton!.click();
    const request = http.expectOne('/api/onboarding/skip');
    expect(request.request.method).toBe('POST');
    expect(request.request.body).toEqual({ step: 'health' });
    request.flush({
      ...pendingState,
      steps: pendingState.steps.map((item) =>
        item.id === 'health'
          ? { ...item, status: 'skipped', skipped: true, resolved: true }
          : item,
      ),
      resolved_count: 2,
    });
    await fixture.whenStable();
    fixture.detectChanges();

    expect(element.textContent).toContain('Skipped for now.');
  });

  it('states when the account is ready', async () => {
    const fixture = await load(readyState);
    const element = fixture.nativeElement as HTMLElement;

    expect(element.textContent).toContain('Your account is ready.');
    expect(element.querySelector('.next-action')).toBeNull();
  });
});
