## 2024-07-06 - Missing Disabled State on AI Chat
**Learning:** Adding a CSS loading class (like 'sending') does not implicitly disable form controls. Without a true `disabled` state, screen readers do not announce the control as disabled, and users can submit duplicate requests via keyboard while a long-running streaming response is active.
**Action:** Always pair visual loading states with explicit `disabled` properties on buttons and inputs, especially for long-running AI requests.
