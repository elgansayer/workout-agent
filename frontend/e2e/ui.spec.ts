import { expect, test, Page } from '@playwright/test';

// All records are synthetic. No production account, provider, or backend is used.
const calendar =
  '<svg class="svg-cal" viewBox="0 0 560 90" role="img" aria-label="Training calendar"><text x="12" y="30" fill="currentColor">Training calendar</text></svg>';
const names = [
  'Cable lateral raise',
  'Reverse fly',
  'Incline dumbbell curl',
  'Overhead triceps extension',
  'Reverse curl',
  'Wrist extension',
  'Biceps curl',
  'Triceps pushdown',
  'Dumbbell extension',
  'Chin up',
  'Hammer curl',
];
const dashboard = {
  week: 2,
  cycle_weeks: 12,
  weekday: 'Monday',
  focus: 'Shoulders & arms',
  block: {
    number: 1,
    name: 'Progressive development',
    focus: 'Build consistent training',
    target_rir: 2,
    rep_emphasis: 'Moderate reps',
    volume_multiplier: 1,
  },
  rows: names.map((name) => ({
    name,
    planned: '3 × 10–12',
    role: 'Accessory',
    last: '12 kg × 12',
    nudge: 'Keep the load steady',
    note: 'Controlled movement',
  })),
  review_lifts: Array.from({ length: 80 }, (_, i) => ({
    name: `Exercise ${i + 1}`,
    trend: ['stalling', 'progressing', 'regressing'][i % 3],
    as_line: '3 logged sessions; review your recent training.',
  })),
  review_headline: '80 exercises reviewed across your training history.',
  dashboard_insight: {
    fatigue: 'Normal',
    wins_stalls: 'Several lifts are improving.',
    advice: 'Follow today’s planned workout.',
  },
  lifestyle: ['Aim for a consistent bedtime.', 'Keep meals regular.'],
  streak: 3,
  rotation_message: 'Your next workout is ready.',
  last_completed_at: '2026-11-01T10:00:00Z',
  calendar,
};
const routines = Array.from({ length: 20 }, (_, i) => ({
  id: `r${i}`,
  title: `Routine ${i + 1}`,
  folder_id: 'folder',
  folder_name: 'Training',
  exercise_count: 11,
  work_set_count: 33,
  estimated_duration_minutes: 60,
  muscle_summary: ['Upper body'],
  source_hash: 'synthetic-source-revision',
  exercises: names.map((title, j) => ({
    template_id: `e${j}`,
    title,
    sets: 3,
    exercise_type: 'weight_reps',
    rest_seconds: 90,
    superset_id: null,
    set_targets: [{ reps: 10, weight_kg: 12 }],
  })),
}));
const days = routines
  .slice(0, 5)
  .map((routine, i) => ({
    routine_id: routine.id,
    number: i + 1,
    focus: routine.title,
    muscles: ['Upper body'],
    estimated_duration_minutes: 60,
    exercises: names.map((name, j) => ({
      name,
      template_id: `e${j}`,
      role: 'Accessory',
      scheme: '3 × 10',
    })),
  }));
const blocks = [
  {
    number: 1,
    name: 'Build',
    weeks: '1–4',
    focus: 'Build a training base',
    is_current: true,
    start_date: '2026-11-02',
    end_date: '2026-11-29',
    volume_multiplier: 1,
    target_rir: 2,
    rep_emphasis: '8–12 reps',
  },
];
const settings = {
  user_keys: { hevy: { has_key: true } },
  user_prefs: {},
  ai_providers: [{ id: 'gemini', name: 'Gemini', default_model: 'synthetic-model' }],
  gh_connected: false,
  gh_configured: false,
  connectors_info: [],
  vapid_public_key: null,
};
const payloads: Record<string, unknown> = {
  '/api/me': {
    user: { id: 'synthetic-user', name: 'Demo athlete', email: 'athlete@example.test' },
  },
  '/api/dashboard': dashboard,
  '/api/history': {
    calendar,
    logs: Array.from({ length: 60 }, (_, i) => ({
      date: '2026-11-01',
      focus: `Session ${i + 1}`,
      day: 1,
      plan: 'Training notes\n'.repeat(20),
    })),
  },
  '/api/checkins': {
    checkins: Array.from({ length: 20 }, (_, i) => ({
      number: i + 1,
      date: '2026-11-01',
      workouts_done: 12,
      weeks: 4,
      message: 'Review notes\n'.repeat(20),
    })),
  },
  '/api/progress': {
    charts: Array.from({ length: 30 }, (_, i) => ({
      name: `Lift ${i + 1}`,
      best_e1rm: 50,
      sessions: 4,
      svg: '<svg class="svg-chart" viewBox="0 0 300 100" aria-label="Progress"><path d="M0 90L100 60L200 40L300 20" stroke="currentColor" fill="none"/></svg>',
    })),
  },
  '/api/stats': {
    total_sessions: 40,
    exercises_tracked: 30,
    total_volume: 20000,
    days_on_programme: 10,
    prs: Array.from({ length: 45 }, (_, i) => ({
      exercise: `Exercise ${i + 1}`,
      e1rm: 50,
      detail: '40 kg × 8',
      date: '2026-11-01',
    })),
  },
  '/api/plan': {
    split_name: 'My Hevy programme',
    week: 2,
    cycle_weeks: 12,
    days,
    blocks,
    rules: ['Follow your routine rotation'],
  },
  '/api/programmes': {
    connection: { state: 'connected' },
    source: {
      routines,
      folders: [{ id: 'folder', title: 'Training' }],
      routine_count: 20,
      recent_workout_count: 40,
    },
    goals: [{ key: 'general_fitness', label: 'General fitness' }],
    duration: { minimum_weeks: 4, maximum_weeks: 24 },
  },
  '/api/settings': settings,
  '/api/chat/history': { messages: [] },
  '/api/csrf-token': { token: 'synthetic-csrf', expires_in: 60 },
};

async function mockApi(page: Page) {
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!(path in payloads)) throw new Error(`Unexpected API request: ${path}`);
    await route.fulfill({ json: payloads[path] });
  });
}
async function noPageOverflow(page: Page) {
  const overflow = await page.evaluate(() => ({
    width: innerWidth,
    scroll: document.documentElement.scrollWidth,
    elements: [...document.querySelectorAll('body *')]
      .filter((el) => {
        const r = el.getBoundingClientRect();
        return r.width && r.right > innerWidth + 1;
      })
      .slice(0, 12)
      .map((el) => ({ tag: el.tagName, cls: el.className })),
  }));
  expect(overflow.scroll, `${page.url()}: ${JSON.stringify(overflow)}`).toBeLessThanOrEqual(
    overflow.width + 1,
  );
}

test.beforeEach(async ({ page }) => {
  await mockApi(page);
});

test('all routes fit the viewport and remain usable with enlarged text', async ({ page }, info) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  for (const route of [
    'dashboard',
    'progress',
    'stats',
    'plan',
    'programmes',
    'history',
    'checkins',
    'chat',
    'settings',
    'profile',
    'login',
  ]) {
    await page.goto(`/${route}`);
    await expect(page.locator('main h1, main h2').first()).toBeVisible();
    await expect(page).toHaveTitle(/Workout Agent/);
    if (info.project.name === 'desktop') {
      await page.screenshot({ path: info.outputPath(`${route}.png`), animations: 'disabled' });
    }
    await noPageOverflow(page);
    await page.addStyleTag({ content: 'html { font-size: 200% !important; }' });
    await noPageOverflow(page);
    if (route === 'dashboard' && info.project.name === 'mobile')
      await page.screenshot({
        path: info.outputPath('dashboard-large-text.png'),
        fullPage: true,
        animations: 'disabled',
      });
  }
  expect(errors).toEqual([]);
});

test('dashboard stays compact and review search can reach the final exercise', async ({
  page,
}, info) => {
  await page.goto('/dashboard');
  await expect(page.locator('.exercise')).toHaveCount(11);
  await expect(page.locator('.coaching-review')).not.toHaveAttribute('open');
  await expect(page.locator('.review-row:visible')).toHaveCount(0);
  if (info.project.name === 'desktop') {
    expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBeLessThan(1500);
  }
  await expect(page.locator('.exercise summary').first()).toBeVisible();
  await page.screenshot({
    path: info.outputPath('dashboard.png'),
    fullPage: true,
    animations: 'disabled',
  });
  const exercise = page.locator('.exercise summary').first();
  await exercise.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('.exercise-detail').first()).toBeVisible();
  await page.getByText('Browse 80 exercise reviews').click();
  await expect(page.locator('.review-row:visible')).toHaveCount(6);
  await page.getByLabel('Search reviews', { exact: true }).fill('Exercise 80');
  await expect(page.locator('.review-row:visible')).toHaveCount(1);
  await expect(page.locator('.review-name:visible')).toHaveText('Exercise 80');
  await page.getByLabel('Search reviews', { exact: true }).fill('');
  await page.getByLabel('Filter reviews by trend').selectOption('regressing');
  await expect(page.locator('.review-trend:visible').first()).toHaveText('regressing');
  await noPageOverflow(page);
});

test('navigation, list paging and settings sections work with keyboard focus', async ({
  page,
}, info) => {
  await page.goto('/dashboard');
  if (info.project.name !== 'desktop') {
    const menu = page.getByRole('button', { name: 'Menu', exact: true });
    await menu.click();
    await page.keyboard.press('Escape');
    await expect(menu).toBeFocused();
    await expect(menu).toHaveAttribute('aria-expanded', 'false');
    await menu.click();
  }
  await page.getByRole('link', { name: 'History', exact: true }).click();
  await expect(page.locator('main')).toBeFocused();
  await expect(page.locator('.log-row')).toHaveCount(8);
  await page.getByRole('button', { name: 'Next logs page' }).click();
  await expect(page.locator('.log-focus').first()).toHaveText('Session 9');
  await page.getByLabel('Search logs', { exact: true }).fill('Session 60');
  await expect(page.locator('.log-row')).toHaveCount(1);
  await page.goto('/settings');
  await expect(page.locator('#settings-hevy')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Connections', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#settings-ai')).toBeHidden();
  await page.getByRole('button', { name: 'AI coach', exact: true }).click();
  await expect(page.locator('#settings-ai')).toBeVisible();
  await page.getByRole('button', { name: 'Profile', exact: true }).click();
  await page.getByLabel('Constraints (optional)').fill('Synthetic preference');
  await page.getByRole('button', { name: 'Connections', exact: true }).click();
  await page.getByRole('button', { name: 'Profile', exact: true }).click();
  await expect(page.getByLabel('Constraints (optional)')).toHaveValue('Synthetic preference');
  await page.getByRole('button', { name: 'Notifications', exact: true }).click();
  await expect(
    page.getByText('Push notifications are not available on this server.'),
  ).toBeVisible();
});

test('routine selection survives pages and edited settings remove the activation preview', async ({
  page,
}) => {
  await page.route('**/api/programmes/preview', (route) =>
    route.fulfill({
      json: {
        preview: {
          name: 'Synthetic programme',
          cycle_weeks: 12,
          analysis: { confidence: 'Moderate' },
          warnings: [],
          blocks,
          days,
          assumptions: [],
          preview_token: 'synthetic-token',
        },
      },
    }),
  );
  await page.goto('/programmes');
  await page.getByRole('checkbox').first().check();
  await page.getByRole('button', { name: 'Next routines page' }).click();
  await expect(page.locator('.selected-routines')).toContainText('Routine 1');
  await page.getByRole('button', { name: 'Generate programme preview' }).click();
  await expect(page.getByRole('button', { name: 'Activate this programme' })).toBeVisible();
  await page.getByLabel('Programme duration').fill('8');
  await expect(page.getByRole('button', { name: 'Activate this programme' })).toHaveCount(0);
});

test('display preferences preserve focus and readable selected states', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/dashboard');
  await expect(page.locator('.today')).toHaveCSS('animation-name', 'none');
  await page.emulateMedia({ forcedColors: 'active' });
  await page.locator('.exercise summary').first().focus();
  await expect(page.locator('.exercise summary').first()).toBeFocused();
  await noPageOverflow(page);
});
