# Display preference accessibility

The Angular frontend treats operating-system display preferences as part of the UI contract. `frontend/src/accessibility-preferences.css` is loaded after the normal application stylesheet and owns the overrides for reduced motion, forced colours, and increased contrast.

## Behaviour contract

### Reduced motion

When `prefers-reduced-motion: reduce` is active, decorative and looping motion is removed, transitions are collapsed, and smooth scrolling is disabled. Loading state remains visible through a static skeleton. State changes must remain understandable from text, borders, icons, or persistent layout; animation must never be the only signal.

### Forced colours

When `forced-colors: active` is active, the application uses system colour keywords such as `Canvas`, `CanvasText`, `Highlight`, `LinkText`, `GrayText`, and `ButtonText`. Decorative gradients/glows are suppressed. Keyboard focus, current navigation, selected routines/providers, warnings, errors, success states, disabled controls, chart labels, and legends retain non-colour cues through outlines, borders, border styles, or text.

Angular navigation links expose `aria-current="page"` through `ariaCurrentWhenActive`, so the current route has both semantic and visual state.

### Increased contrast

When `prefers-contrast: more` is active, foreground/background separation increases, borders become heavier, focus indicators become thicker, and selected/error/warning/success states gain persistent outlines or left borders.

## Automated contract

Run from `frontend/`:

```bash
npm run test:a11y-preferences
npm test -- --watch=false
npm run build
```

`test:a11y-preferences` is dependency-free and deterministic. It verifies that:

- the preference stylesheet is loaded after the base stylesheet;
- all three media queries remain present;
- reduced-motion rules cover ambient animation, loading, chat, cards, and scrolling;
- forced-colour rules retain focus, current/selected state, errors, warnings, success, forms, charts, and legends;
- all concrete Angular component routes are represented in `frontend/accessibility-preferences.matrix.json`;
- every primary navigation link using `routerLinkActive` exposes `ariaCurrentWhenActive="page"`.

Adding a component route therefore requires updating the accessibility matrix before CI passes.

## Manual verification matrix

Use a current Chromium/Edge build for forced colours and DevTools rendering emulation where supported. Also perform at least one Windows High Contrast pass because forced-colour rendering is platform-controlled.

| Area | Reduced motion | Forced colours | Increased contrast |
| --- | --- | --- | --- |
| Shared navigation and skip link | No animated underline movement is needed to identify the route; focus jumps directly | Current page has semantic `aria-current` plus a persistent highlight border; keyboard focus is visible | Current page and focus have thick outlines |
| Buttons, links, forms, details/summary | Hover/press transitions are effectively immediate | Boundaries use system colours; disabled controls remain visibly disabled; focus survives | Controls have heavier borders and focus outlines |
| Loading and streaming | Skeleton is static; ambient glow, typing bounce, streaming cursor, reveal, and floating icon animation are removed | Loading/streaming content remains textually understandable | Loading boundaries remain visible |
| Selected/current cards | Selection does not depend on movement or glow | Current block/day/programme, selected routine, and active provider gain persistent border/outline | Selection gains persistent high-contrast outline |
| Errors, warnings, success | No animation is required to notice status | Double, dashed, and solid border treatments distinguish status without hue | Heavy left-border treatments supplement colour |
| Charts and legends | Chart comprehension is unchanged because no chart state depends on animation | Labels use system foreground and legend swatches retain borders | Muted chart labels are promoted to higher contrast |

### Critical routes

Verify each route at 100% and 200% browser zoom, keyboard-only, and with the three preference modes above. `frontend/accessibility-preferences.matrix.json` is the canonical route list and is checked against `app.routes.ts` automatically.

| Route | Key states to inspect |
| --- | --- |
| `/login` | focus, form controls, validation/error presentation |
| `/profile` | form controls, muted metadata, save/status feedback |
| `/dashboard` | hero/cards, loading, charts, current navigation |
| `/chat` | welcome state, suggestions, streaming/typing, send/stop controls, message boundaries |
| `/checkins` | cards/tables, status badges, current navigation |
| `/history` | log rows, metadata, current navigation |
| `/plan` | current block/day, badges, exercise rows |
| `/programmes` | selected routine, active programme, warnings/errors/success, filters, ordering controls |
| `/progress` | charts, legends, axis/value labels |
| `/settings` | provider selection, credential inputs, success/error status, disabled states |
| `/stats` | charts, cards, legends, current navigation |

## Review rules

Do not add animation as the sole indication of loading, selection, success, error, or navigation state. Do not opt important controls or data visualisations out of forced-colour adjustment unless an equivalent system-colour fallback and non-colour cue are provided. New routes must be added to the matrix and manually checked before release.
