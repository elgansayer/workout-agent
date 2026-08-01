## 2023-10-27 - Adding focus states
**Learning:** In CSS, adding `border-radius: inherit` to focused elements is dangerous. If a circular button (`border-radius: 50%`) inside a standard container receives focus, it inherits a `0px` border-radius from its parent and suddenly turns into a square. Modern browsers automatically curve `outline` to match an element's existing `border-radius`, so modifying `border-radius` on focus is unnecessary.
**Action:** Simply use `outline` for focus states and avoid modifying `border-radius` directly for focus indicators.
