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

## 2025-03-03 - AI Loading States and Aria Attributes Pattern
**Learning:** For dynamic AI requests in this design system (like the "Why did this happen?" button), buttons often lack `aria-expanded`, `aria-controls` bindings to their response containers, and visual/functional loading states (e.g. disabling the button) which can result in duplicate requests and poor screen reader experience.
**Action:** Always bind dynamic explanation buttons to their result containers using `aria-controls` and `aria-expanded`. Use `aria-live="polite"` on the result container to read out the content dynamically. Disable the button during async fetch and restore it in the `finally` block to prevent redundant clicks.
## 2024-10-24 - Add ARIA Labels to Missing Icons and Form Inputs
**Learning:** Found an accessibility issue pattern in the app's components where icon-only buttons rely on `title` attributes instead of explicit `aria-label`, and placeholder-only inputs lack proper `aria-label` definitions, degrading screen reader experience.
**Action:** Always add `aria-label` to icon-only buttons (while hiding decorative SVGs with `aria-hidden="true"`) and provide explicit labels or `aria-label` tags to input fields lacking a visible `<label>`.

## 2023-11-20 - Custom Chip Selector Accessibility
**Learning:** Custom interactive elements, like selection chips built with `<button>` elements, rely entirely on CSS classes like `.selected` for visual state. Screen readers cannot detect these visual states, making custom selections inaccessible.
**Action:** Always wrap custom selections in a `role="group"` container with `aria-labelledby` pointing to their logical group label. Dynamically toggle the `aria-pressed="true"` / `"false"` attribute concurrently with the visual CSS classes (both in the server-rendered template and in the client-side JavaScript toggle logic) to ensure full accessibility for stateful custom components.
## 2024-10-24 - Accessible custom toggle chips
**Learning:** When implementing custom toggleable elements (like selection chips) using unsemantic elements such as `<div>` and `<button>` instead of radio inputs or checkboxes, they lack native accessibility grouping and toggle semantics.
**Action:** Use `role="group"` on the container with an `aria-labelledby` linking it to a descriptive label. Provide `aria-pressed="true"` or `aria-pressed="false"` dynamically to the inner toggleable `<button>` elements to communicate their selected state to screen readers.
