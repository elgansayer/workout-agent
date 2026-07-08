## 2024-07-08 - Loading States for Interactive Elements
**Learning:** `pointer-events: none` on loading buttons is insufficient for keyboard accessibility, as focus can sometimes bypass styling states and screen readers won't announce the state change correctly.
**Action:** Always include actual `disabled` attributes alongside ARIA labels for icon-only interactive elements and toggle the disabled state in JS during loading or streaming logic.
