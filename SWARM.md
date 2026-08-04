# SWARM.md — Operating the workout-agent AI Swarm

This is the operational quick-start for the autonomous coding system
(`swarmd.py`, controlled via `./swarmctl`). Read `AGENTS.md` §9 first for the
architecture; this doc is "how do I actually turn it on."

## Before you turn this on — read this

**The swarm auto-pushes directly to `origin/main`, unattended, and its
failure-handling path (`discard_working_tree_changes()`) runs `git checkout
-- .` + `git clean -fd` on the *whole working tree* when a task can't pass
verification.** That means:

1. **Commit or stash any uncommitted work before starting the swarm.** As of
   this write-up there is real in-progress work sitting uncommitted in this
   repo (`database.py`, `hevy_client.py`, `webapp/app.py` modifications, plus
   new `ai_provider.py`/`encryption.py`/`hevy_reader.py`/`programme_inference.py`
   files) — the test suite currently has 10 failures / 14 errors because of
   it. This is already the #1 seeded task (`.tasks/pending/0001-*`), but if
   the swarm's first attempt at *any* task fails verification before that
   uncommitted work is either fixed or committed, discard could wipe it.
   **Commit what you have (even as a WIP commit) before running `./swarmctl
   start` for the first time.**
2. Every verified task becomes a real commit on `main`, pushed immediately.
   There is no PR/review step in this configuration. If you want a review
   step instead, change `git_commit(..., push=True)`'s call sites in
   `swarmd.py` to push to a branch and open a PR — that's a deliberate
   config choice this repo currently does *not* make.
3. The swarm will run AI coding CLIs with `--dangerously-skip-permissions`
   (Claude/Antigravity) and `--yes-always` (aider) — it has full write access
   to this repo and will run arbitrary shell commands the model chooses to
   run. Only run this against a repo/host you're comfortable with that.

## One-time setup

1. Copy `.env.swarm.example` → `.env.swarm` and fill in whichever model
   keys you want the swarm's *own coding* to use (separate from the
   product's own `.env` — see `.env.swarm.example`'s header comment for the
   distinction). On this host, `GEMINI_API_KEY`/`OPENAI_API_KEY`/
   `DEEPSEEK_API_KEY` are already present in the shell environment and
   `claude`/`agy`/`aider`/`gh` are all installed and on `PATH` — run
   `./swarmctl health` to confirm current status.
2. `aider` (needed for the `copilot`/`deepseek` model runners) is installed
   via `uv tool install --python 3.12 aider-chat` in an isolated environment
   — this project's own `.venv` runs Python 3.14, which aider's
   numpy/scipy dependency chain doesn't yet ship wheels for. If you rebuild
   the venv or move hosts, reinstall aider the same way rather than adding
   it to `requirements.txt`.
3. `ruff`/`mypy` are installed into `.venv` (added to `requirements.txt`'s
   dev section) — `swarmd.py` invokes them as `<venv-python> -m ruff`/
   `-m mypy`, not bare `ruff`/`mypy` on `PATH`, so they work correctly even
   though the swarm is normally launched via `swarmctl`/systemd rather than
   an activated shell.
4. Ensure the `ai-agent-task`/`hourly`/`daily`/`weekly` GitHub labels exist
   on the remote repo (already created as part of this setup) — required
   for `.github/workflows/agent-*.yml` to open issues and for `swarmd.py`'s
   `sync_github_issues()` to find them.

## Starting / stopping

```bash
./swarmctl start          # tmux session, easiest to watch live
./swarmctl attach          # watch it work (Ctrl+B, D to detach)
./swarmctl status          # health + task queue + last commit, no attach needed
./swarmctl logs             # last 50 lines of logs/swarmd.log
./swarmctl stop             # stop everything, clear the coordination lock
./swarmctl once              # run exactly one cycle then exit (good for testing/cron)
./swarmctl health            # check CLIs, keys, ruff/mypy/pytest, gh auth, git state
```

For a real always-on deployment, install the systemd units instead of tmux:

```bash
mkdir -p ~/.config/systemd/user
cp config/systemd/swarmd.service config/systemd/swarm-watchdog.service \
   config/systemd/swarm-watchdog.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now swarmd.service
systemctl --user enable --now swarm-watchdog.timer
journalctl --user -u swarmd.service -f     # tail logs
```

(`loginctl enable-linger $USER` if you want it to keep running after you log
out.) `swarmd.service` has `Restart=always`; the watchdog timer runs every 5
minutes and force-restarts the tmux session or the systemd service if it
detects a dead process, a stale heartbeat, or no commits for 15+ minutes.

## What happens once it's running

1. `.tasks/pending/` is drained FIFO (lowest phase number, then sequence).
   18 tasks are pre-seeded as of this setup — see `.tasks/pending/*.task`,
   summarised in `AGENTS.md` §7's Known Issues and prioritised: failing
   tests first, then AI-provider wiring, then multi-tenant migration
   table-by-table, then the programme-builder UI, then scheduler
   consolidation, then docs/config hygiene.
2. Each task is tried against `claude` → `antigravity` → `copilot` →
   `deepseek` in rotating order until one produces real file changes.
3. The result runs through the verification gate (`ruff`, `pytest`,
   `webapp.app`/`main` import sanity) — on failure it gets up to 5 automated
   fix rounds; if still failing, changes are discarded and the task is
   parked in `.tasks/stuck/` (check there periodically — it needs you, not
   the swarm).
4. On success: commit, push to `main`, a lightweight self-review scans the
   diff for missing tests/bare excepts/possible secret leaks and queues
   follow-up tasks if it finds any.
5. When `.tasks/pending/` is empty, it runs the full test suite as a
   regression check, and either queues a fix task or invents a new
   audit/review task from a rotating list (`AUDIT_PROMPTS` in `swarmd.py`)
   — it never runs out of things to do.
6. `.github/workflows/agent-{hourly,daily,weekly}.yml` open GitHub issues
   from `.agents/automations/*.md` on schedule; `swarmd.py` polls for them
   (`sync_github_issues()`, every 20 min by default) and imports them into
   the same queue, closing the issue on completion.

## Checking in on it

- `.tasks/stuck/` — tasks that genuinely couldn't pass verification after
  retries. These need a human, not another swarm cycle.
- `git log --author="AI Swarm"` — everything the swarm has shipped.
- Telegram alerts fire (if `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are set)
  on: no progress for 15 minutes, no progress for 45 minutes (triggers a
  forced recovery restart), and any task landing in `.tasks/stuck/`.
- `./swarmctl status` any time for a one-shot snapshot without attaching.

## Known environment caveats (as of this setup)

- Host disk usage was at ~94% when this was set up — `watchdog.sh` will
  aggressively clean caches at 85%+, but a long-running swarm generates a
  lot of `__pycache__`/`.pytest_cache`/aider scratch files on top of an
  already-full disk. Worth checking `df -h` before a long unattended run.
- `ANTHROPIC_API_KEY` is not set in the environment, but the `claude` model
  runner doesn't need it (the `claude` CLI uses its own separate login) —
  this only matters if you also want `ClaudeProvider` in `ai_provider.py`
  (the *product's* AI-provider abstraction, unrelated to the swarm's own
  coding CLI) to work for end users bringing their own Claude key.
