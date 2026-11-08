import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Settings } from './settings';

const providers = [
  { id: 'gemini', name: 'Google Gemini', default_model: 'gemini-3.8-flash' },
  { id: 'claude', name: 'Anthropic Claude', default_model: 'claude-sonnet-4-20250514' },
  { id: 'openai', name: 'OpenAI', default_model: 'gpt-4o' },
  { id: 'deepseek', name: 'DeepSeek', default_model: 'deepseek-chat' },
];

const settingsResponse = {
  user_prefs: {
    preferred_ai: 'claude',
    ai_model: 'claude-selected',
    goals: ['strength'],
    experience_level: 'advanced',
    constraints: [],
  },
  user_keys: {
    hevy: { has_key: false, masked: null, model: null },
    gemini: { has_key: false, masked: null, model: null },
    claude: { has_key: true, masked: '••••••••key1', model: 'claude-stored' },
    openai: { has_key: false, masked: null, model: null },
    deepseek: { has_key: false, masked: null, model: null },
  },
  ai_providers: providers,
  training_rules: {
    version: 2,
    prohibited_exercises: [{ exercise: 'Behind The Neck Press', reason: 'Shoulder impingement' }],
    substitutions: [
      { from_exercise: 'Barbell Back Squat', to_exercise: 'Leg Press', reason: 'Knee strain' },
    ],
    rep_intensity_bounds: { min_reps: 5, max_reps: 20, max_rpe: 8.5, min_rir: 1.5 },
    frequency: { max_sessions_per_week: 4, min_rest_days_between_sessions: 1 },
    volume: { max_sets_per_workout: 18, max_sets_per_exercise: 4 },
    equipment: {
      available_equipment: ['barbell', 'dumbbells'],
      prohibited_equipment: ['smith_machine'],
    },
    recovery: { min_sleep_hours: 7.5, min_readiness_score: 65, auto_deload_on_fatigue: true },
    contextual_notes: [
      { category: 'joint', note: 'Right shoulder sensitivity', severity: 'warning' },
    ],
  },
  gh_connected: false,
  gh_configured: false,
  connectors_info: [],
  vapid_public_key: null,
};

describe('Settings AI provider parity', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Settings],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function loadSettings(): Promise<ComponentFixture<Settings>> {
    const fixture = TestBed.createComponent(Settings);
    fixture.detectChanges();
    http.expectOne('/api/settings').flush(settingsResponse);
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  it('renders every provider and default model supplied by the registry API', async () => {
    const fixture = await loadSettings();
    const element = fixture.nativeElement as HTMLElement;
    const labels = Array.from(element.querySelectorAll('.provider-tab')).map((button) =>
      button.textContent?.replace(/\s+/g, ' ').trim(),
    );

    expect(labels).toEqual(['✦ Google Gemini', '⬡ Anthropic Claude', '◉ OpenAI', '◉ DeepSeek']);
    expect(element.querySelector<HTMLInputElement>('#deepseek-model')?.placeholder).toBe(
      'deepseek-chat',
    );
    expect(element.querySelector<HTMLInputElement>('#claude-model')?.value).toBe('claude-selected');
  });

  it('persists the selected provider and its current model preference', async () => {
    const fixture = await loadSettings();
    const component = fixture.componentInstance;
    component.selectProvider('openai');
    component.aiModelInputs['openai'] = 'gpt-synthetic';

    component.savePreferences();

    const request = http.expectOne('/api/settings/preferences');
    expect(request.request.method).toBe('POST');
    expect(request.request.body.preferred_ai).toBe('openai');
    expect(request.request.body.ai_model).toBe('gpt-synthetic');
    request.flush({ status: 'ok' });
  });
  it('does not discard unsaved profile edits after saving a connector key', async () => {
    const fixture = await loadSettings();
    const component = fixture.componentInstance;
    component.constraintsInput = 'Keep this unsaved change';
    component.selectedGoals.set(['power']);
    component.hevyKeyInput = 'synthetic-test-key';
    component.saveKey('hevy');
    http.expectOne('/api/settings/key').flush({ status: 'ok' });
    http.expectOne('/api/settings').flush(settingsResponse);
    expect(component.constraintsInput).toBe('Keep this unsaved change');
    expect(component.selectedGoals()).toEqual(['power']);
    expect(component.hevyKeyInput).toBe('');
  });

  it('explains unsupported notifications instead of silently failing', async () => {
    const fixture = await loadSettings();
    await fixture.componentInstance.subscribeToPush();
    expect(fixture.componentInstance.pushStatus()).toContain('not supported');
    expect(fixture.componentInstance.pushBusy()).toBe(false);
  });

  it('disconnects Google Health through HttpClient so the CSRF interceptor is used', async () => {
    const fixture = await loadSettings();
    fixture.componentInstance.disconnectGoogleHealth();
    fixture.componentInstance.disconnectGoogleHealth();
    const request = http.expectOne('/google-health/disconnect');
    expect(request.request.method).toBe('POST');
    expect(request.request.responseType).toBe('text');
    request.flush('');
    http.expectOne('/api/settings').flush(settingsResponse);
    expect(fixture.componentInstance.healthStatus()).toContain('disconnected');
  });

  it('loads and displays structured training rules in the accessible editor', async () => {
    const fixture = await loadSettings();
    const component = fixture.componentInstance;
    expect(component.trainingRules()?.version).toBe(2);
    expect(component.prohibitedExercises().length).toBe(1);
    expect(component.prohibitedExercises()[0].exercise).toBe('Behind The Neck Press');
    expect(component.substitutions().length).toBe(1);
    expect(component.substitutions()[0].to_exercise).toBe('Leg Press');
    expect(component.repBounds().min_reps).toBe(5);
    expect(component.repBounds().max_reps).toBe(20);
    expect(component.volume().max_sets_per_workout).toBe(18);
    expect(component.equipment().available).toContain('barbell');
    expect(component.recovery().min_sleep_hours).toBe(7.5);
    expect(component.contextualNotes().length).toBe(1);

    const element = fixture.nativeElement as HTMLElement;
    expect(element.querySelector('#rule-version-badge')?.textContent).toContain('v2');
  });

  it('allows adding and removing prohibited exercises, substitutions, and notes', async () => {
    const fixture = await loadSettings();
    const component = fixture.componentInstance;

    component.addProhibitedExercise();
    expect(component.prohibitedExercises().length).toBe(2);
    component.removeProhibitedExercise(0);
    expect(component.prohibitedExercises().length).toBe(1);

    component.addSubstitution();
    expect(component.substitutions().length).toBe(2);
    component.removeSubstitution(0);
    expect(component.substitutions().length).toBe(1);

    component.addContextualNote();
    expect(component.contextualNotes().length).toBe(2);
    component.removeContextualNote(0);
    expect(component.contextualNotes().length).toBe(1);
  });

  it('saves structured training rules via dedicated rules endpoint', async () => {
    const fixture = await loadSettings();
    const component = fixture.componentInstance;

    component.saveTrainingRules();

    const request = http.expectOne('/api/settings/rules');
    expect(request.request.method).toBe('POST');
    expect(request.request.body.version).toBe(2);
    expect(request.request.body.prohibited_exercises[0].exercise).toBe('Behind The Neck Press');
    expect(request.request.body.substitutions[0].from_exercise).toBe('Barbell Back Squat');

    request.flush({
      status: 'ok',
      rules: {
        ...request.request.body,
        version: 3,
      },
    });

    expect(component.rulesStatus()).toContain('Rules saved (v3)');
    expect(component.trainingRules()?.version).toBe(3);
  });

  it('includes structured rules in savePreferences profile payload', async () => {
    const fixture = await loadSettings();
    const component = fixture.componentInstance;

    component.savePreferences();

    const request = http.expectOne('/api/settings/preferences');
    expect(request.request.method).toBe('POST');
    expect(request.request.body.training_rules).toBeDefined();
    expect(request.request.body.training_rules.version).toBe(2);
    expect(request.request.body.training_rules.rep_intensity_bounds.min_reps).toBe(5);
    request.flush({ status: 'ok' });
  });
});
