# Hourly Commit Hygiene

## Objective
Keep the git history clean and make sure nothing sensitive ever lands in a
commit.

## Instructions
1. Review the last 10 commits (`git log -10 --stat`). Confirm commit
   messages are descriptive (not just "fix" or "wip") — if the swarm
   produced a low-quality message recently, that's informational only, don't
   rewrite published history.
2. Check `git log -p -10 -- .env .env.* data/ '*.db'` returns nothing — no
   environment file or database file should ever have been committed. If one
   was, do not force-push history rewrites unattended; instead add the
   offending path to `.gitignore` if missing, remove the file from the
   current tree, and file a `task_add()` flagged "SECURITY: secret possibly
   committed in <sha>, needs human-supervised history rewrite" — this is
   explicitly a human-in-the-loop action, not something to auto-resolve.
3. Confirm `.gitignore` still covers `*.db`, `.env`, `__pycache__/`,
   `.pytest_cache/`, `.venv/` — add any missing entries.
4. Confirm no file larger than a few MB was added outside `data/` (which is
   gitignored) — a stray binary/log file committed by mistake bloats the
   repo permanently.
