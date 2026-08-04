# Daily Documentation Sync

## Objective
Keep README.md, `.env.example`, and `AGENTS.md`'s "Known Issues" section
honest about the current state of the code — docs-vs-code drift (like the
README claiming there's no login when there is one) erodes trust in every
other doc in the repo.

## Instructions
1. Diff README.md's claims against the actual code for anything that changed
   in the last 24 hours: new routes, new env vars, new auth requirements,
   new port numbers in `docker-compose.yml`/`docker-compose.portainer.yml`.
2. Specifically check the previously-known drift points: web app login
   behaviour, dashboard port number consistency across README and both
   compose files, and any `.env.example` variable that's read in code but
   undocumented (or documented but no longer read anywhere).
3. Update README.md/`.env.example` to match reality. Do not invent
   aspirational features in the docs that don't exist in code yet — if
   something is planned but not built, it belongs in a `task_add()` entry or
   `AGENTS.md`'s Known Issues list, not presented as current functionality.
4. If `AGENTS.md` §7 (Known Issues) lists something that's now fixed, update
   it to reflect that (strike-through or remove, matching how other swarm
   constitutions mark resolved findings) rather than leaving stale findings
   accumulating indefinitely.
