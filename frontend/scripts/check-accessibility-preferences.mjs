import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const scriptsDir = dirname(fileURLToPath(import.meta.url));
const frontendRoot = dirname(scriptsDir);
const read = (relativePath) => readFileSync(join(frontendRoot, relativePath), 'utf8');

const css = read('src/accessibility-preferences.css');
const angular = JSON.parse(read('angular.json'));
const matrix = JSON.parse(read('accessibility-preferences.matrix.json'));
const routesSource = read('src/app/app.routes.ts');
const appTemplate = read('src/app/app.html');

const styles = angular.projects.frontend.architect.build.options.styles;
assert.deepEqual(
  styles.slice(-2),
  ['src/styles.css', 'src/accessibility-preferences.css'],
  'accessibility-preferences.css must load after the base stylesheet',
);

const requiredMediaQueries = [
  '@media (prefers-reduced-motion: reduce)',
  '@media (forced-colors: active)',
  '@media (prefers-contrast: more)',
];
for (const query of requiredMediaQueries) {
  assert.ok(css.includes(query), `missing accessibility media query: ${query}`);
}
assert.deepEqual(matrix.preferences, requiredMediaQueries.map((query) => query.slice(8, -1)));

const requiredReducedMotionTokens = [
  'animation: none !important',
  'transition-duration: 0.01ms !important',
  'scroll-behavior: auto !important',
  '.skeleton',
  '.chat-bubble',
];
for (const token of requiredReducedMotionTokens) {
  assert.ok(css.includes(token), `reduced-motion contract is missing ${token}`);
}

const requiredForcedColourSelectors = [
  ':focus-visible',
  '[aria-current="page"]',
  '[aria-selected="true"]',
  '.routine-card.selected',
  '.provider-tab.active',
  '.error-panel',
  '.warning-list',
  '.success-panel',
  '.svg-chart',
  '.legend-dot',
];
for (const selector of requiredForcedColourSelectors) {
  assert.ok(css.includes(selector), `forced-colour contract is missing ${selector}`);
}

const requiredSystemColours = ['Canvas', 'CanvasText', 'Highlight', 'LinkText', 'GrayText', 'ButtonText'];
for (const systemColour of requiredSystemColours) {
  assert.ok(css.includes(systemColour), `forced-colour contract is missing ${systemColour}`);
}

const discoveredRoutes = [...routesSource.matchAll(/\{\s*path:\s*'([^'*]+)'\s*,\s*component:/g)]
  .map((match) => match[1])
  .sort();
const documentedRoutes = [...matrix.routes].sort();
assert.deepEqual(
  documentedRoutes,
  discoveredRoutes,
  'manual accessibility matrix must cover every concrete Angular component route',
);

const activeNavigationLinks = [...appTemplate.matchAll(/<a\b[^>]*routerLinkActive="on"[^>]*>/g)].map(
  (match) => match[0],
);
assert.ok(activeNavigationLinks.length >= 1, 'expected active navigation links to audit');
for (const link of activeNavigationLinks) {
  assert.match(
    link,
    /ariaCurrentWhenActive="page"/,
    `active navigation link must expose page state to assistive technology: ${link}`,
  );
}
assert.match(appTemplate, /<nav\s+aria-label="Primary navigation">/);

const expectedSharedChecks = [
  'focus-visible',
  'current-or-selected-state',
  'error-warning-success-state',
  'form-controls',
  'charts-and-legends',
  'loading-and-streaming-state',
];
assert.deepEqual(matrix.sharedChecks, expectedSharedChecks);

console.log(
  `Accessibility preference contract OK: ${documentedRoutes.length} routes, ` +
    `${matrix.sharedChecks.length} shared checks, ${matrix.preferences.length} preference modes.`,
);
