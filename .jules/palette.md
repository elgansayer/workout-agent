## 2024-07-07 - Add ARIA Labels to Chat Input and Icon-only Buttons
**Learning:** Found inputs (like textareas) relying solely on placeholders instead of proper labels, and icon-only buttons (like modal close buttons, clear buttons, send buttons) lacking screen-reader accessible names in this app's components, causing accessibility issues. Also, decorative SVGs were missing `aria-hidden="true"`.
**Action:** Consistently check and add `aria-label` to textareas and icon-only buttons, and use `aria-hidden="true"` on decorative icons inside these elements across the app.
