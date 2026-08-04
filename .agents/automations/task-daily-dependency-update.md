# Daily Dependency Check

## Objective
Keep dependencies current and free of known vulnerabilities without
destabilising the build.

## Instructions
1. Compare pinned floors in `requirements.txt`/`requirements-web.txt`
   against latest published versions on PyPI for each package.
2. If a security advisory exists for any pinned package (check via `pip
   index versions <pkg>` plus release notes, or a vulnerability DB if
   reachable), bump the floor and note the CVE/advisory in the commit
   message.
3. For routine (non-security) updates, bump at most one or two packages per
   run rather than mass-upgrading everything at once — a failing test after
   a single-package bump is much easier to diagnose than after ten.
4. After bumping, run the `verification-gate` skill's full steps. If the
   bump breaks tests or imports, revert that specific bump rather than
   trying to fix forward into unrelated code, and note in the commit why it
   was reverted.
