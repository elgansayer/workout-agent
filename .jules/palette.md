## 2023-10-27 - Adding focus states
**Learning:** In CSS, adding `border-radius: inherit` to focused elements is dangerous. If a circular button (`border-radius: 50%`) inside a standard container receives focus, it inherits a `0px` border-radius from its parent and suddenly turns into a square. Modern browsers automatically curve `outline` to match an element's existing `border-radius`, so modifying `border-radius` on focus is unnecessary.
**Action:** Simply use `outline` for focus states and avoid modifying `border-radius` directly for focus indicators.

## 2024-07-08 - Loading States for Interactive Elements
**Learning:** `pointer-events: none` on loading buttons is insufficient for keyboard accessibility, as focus can sometimes bypass styling states and screen readers won't announce the state change correctly.
**Action:** Always include actual `disabled` attributes alongside ARIA labels for icon-only interactive elements and toggle the disabled state in JS during loading or streaming logic.

## 2025-03-03 - Missing ARIA Labels on Interactive Elements
**Learning:** Found several icon-only buttons (like the chat send button and modal close button) and text inputs without proper ARIA labels. This is a common accessibility anti-pattern that makes screen readers unable to convey the purpose of these elements.
**Action:** Always ensure that any interactive elements, especially those relying purely on visual cues (like SVG icons or close symbols like `&times;`) or inputs without an associated explicit `<label>`, include descriptive `aria-label` attributes to ensure they are fully accessible to screen reader users.

## 2025-03-03 - Missing Keyboard Focus Indicators
**Learning:** Found that interactive elements (buttons, links, inputs, textareas) lacked a visible `:focus-visible` state. This makes keyboard navigation very difficult for users who rely on it, as they can't tell which element currently has focus.
**Action:** Added a global `:focus-visible` rule in `style.css` using the existing `--accent` color to ensure high contrast, accessible focus indicators are present for keyboard users across the entire application without affecting mouse users.
