## 2024-05-29 - Global focus-visible styling
**Learning:** Adding a generic `:focus-visible` to `webapp/static/style.css` provides clear focus states without disrupting mouse interactions. Using `outline-offset: 2px` prevents clipping inside standard borders, which is crucial for accessibility.
**Action:** Apply global focus rings using existing design tokens (`var(--accent)`) by default on new projects to ensure keyboard-first a11y out of the box.
