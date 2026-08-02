## 2025-03-03 - Missing ARIA Labels on Interactive Elements
**Learning:** Found several icon-only buttons (like the chat send button and modal close button) and text inputs without proper ARIA labels. This is a common accessibility anti-pattern that makes screen readers unable to convey the purpose of these elements.
**Action:** Always ensure that any interactive elements, especially those relying purely on visual cues (like SVG icons or close symbols like `&times;`) or inputs without an associated explicit `<label>`, include descriptive `aria-label` attributes to ensure they are fully accessible to screen reader users.
## 2025-03-03 - Missing Keyboard Focus Indicators
**Learning:** Found that interactive elements (buttons, links, inputs, textareas) lacked a visible `:focus-visible` state. This makes keyboard navigation very difficult for users who rely on it, as they can't tell which element currently has focus.
**Action:** Added a global `:focus-visible` rule in `style.css` using the existing `--accent` color to ensure high contrast, accessible focus indicators are present for keyboard users across the entire application without affecting mouse users.
