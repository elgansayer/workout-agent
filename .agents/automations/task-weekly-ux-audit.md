# Weekly UX & Dead-Button Audit

## Objective
Make sure the dashboard reads as a real product, not a half-wired prototype
— no dead buttons, no silent no-ops, no page that only makes sense to
someone who already knows the codebase.

## Instructions
1. Walk every page under `webapp/templates/` (`dashboard.html`,
   `settings.html`, `plan.html`, `progress.html`, `stats.html`,
   `history.html`, `checkins.html`, `chat.html`) and grep for `<button`,
   `<a `, and `hx-`/`onclick`-style interactive elements. Confirm every one
   either hits a real route, submits a real form, or is explicitly
   disabled/hidden with a reason — no button that does nothing when clicked.
2. Check mobile viewport rendering (the app is PWA-installable per
   `manifest.webmanifest`/`sw.js`) — resize/test at a phone-width viewport
   for each page, note any overflow, unreadable text, or unreachable
   controls.
3. Confirm empty states are handled everywhere real user data might not
   exist yet (a brand-new user with no Hevy history, no body metrics logged,
   no chat history) — the dashboard must never render a raw error or a blank
   page for a first-time user; it should show a clear "connect your Hevy
   account" / "log your first check-in" prompt instead.
4. Fix small, unambiguous issues directly. For larger UX gaps (e.g. the
   missing programme-selection flow — see the `programme-builder-ui` skill),
   file a clearly-scoped `task_add()` entry rather than a rushed partial
   implementation.
