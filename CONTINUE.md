# Continuation Prompt: Autonomous Workout Agent

> Paste this whole file to a coding agent (GitHub Copilot, etc.) opened in this
> project to continue the build. It contains the full context, current state,
> and the next tasks.

---

## Role

You are an expert Python engineer continuing work on an existing local project.
Read the codebase first, keep changes small and focused, and do not rewrite
working code without reason. Match the existing style (type hints, module-level
logging, British English, never use the em dash).

## What this project is

A private, local Python agent that acts as an elite hypertrophy / stage-prep
bodybuilding coach. Each morning it:

1. Pulls the latest workout from the **Hevy API**.
2. Reads recovery metrics (sleep, weight, resting HR) from a JSON file exported
   from **Google Health Connect**.
3. Asks **Google Gemini** to apply progressive overload and pick a daily tip.
4. Sends tomorrow's exact routine to the user's phone via a **Telegram** bot.
5. Advances the position in a 6-day training cycle stored in **SQLite**.

It is deliberately a small focused script, not a generalist agent framework.

## The trainee & non-negotiable coaching rules

The athlete (Elgan) wants a Greek-god physique (broad shoulders, wide lats,
V-taper, narrow waist) and fat loss. The coach persona must always honour:

- Hypertrophy, not maximal strength. No heavy barbell maxes.
- Strict 3-second negative (eccentric) on every rep. No momentum.
- Rep ranges 10 to 20, sets close to failure.
- Pre-exhaust with an isolation movement before any compound.
- No Bulgarian split squats (bad toes). Use the flat-foot leg press.
- No stomach vacuums. Train abs for mass with progressive overload.
- Favour lateral and rear-delt isolation over heavy overhead pressing (he also
  does Thai boxing and bouldering, so shoulders and elbows need protecting).
- Protein around 2 g per kg of bodyweight to preserve muscle in a deficit.
- British English spelling. Never use the em dash.

These rules live in `program.py` as `COACHING_RULES` alongside the structured
6-day split. Keep them in sync if you change the programme.

## Current file structure

```
workout-agent/
├── main.py              # Orchestrates the daily run
├── config.py            # Loads secrets from .env via environment variables
├── database.py          # SQLite: programme_state + workout_history
├── program.py           # The perfected 6-day split as structured data + rules
├── hevy_client.py       # Pulls latest workout from the Hevy API (timeouts, errors)
├── health_connect.py    # Reads sleep/weight JSON exported from Health Connect
├── gemini_engine.py     # Builds the prompt, calls Gemini, falls back to baseline
├── telegram_notifier.py # Sends the message (handles the 4096-char limit)
├── requirements.txt
├── .env.example         # Template; copy to .env (git-ignored)
└── .gitignore
```

## Current state (working and verified)

- Modules compile cleanly; the SQLite cycle seeds at day 1 and advances 1..6..1.
- Secrets are loaded from a git-ignored `.env` (never committed).
- All outbound HTTP calls use explicit timeouts and degrade gracefully.
- If Hevy, Health Connect, or Gemini are unavailable, the agent still sends the
  baseline plan rather than crashing.
- Gemini model is configurable via `GEMINI_MODEL` (default `gemini-2.0-flash`).

## How to run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in HEVY_API_KEY, GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
python main.py
```

Scheduled via cron at 07:00 daily (see README).

## Next tasks (prioritised)

Work through these one at a time. After each, run a quick check and keep the
commit small.

1. **`--preview` / dry-run flag** in `main.py`: print the generated plan to
   stdout without sending Telegram or advancing the day. Add `argparse`.
2. **Tests**: add `pytest`. Cover `database.py` (seed, get, advance/wrap at 6),
   `program.py` (day_focus/format_day for all 6 days), and
   `health_connect.read_recovery_metrics` (missing file, bad JSON, valid file).
   Use a temp DB path and temp files; do not hit any network.
3. **Parse the Hevy payload** into a compact summary (exercise name, top set
   weight x reps, and whether the top of the rep range was reached) before
   sending to Gemini, instead of dumping raw JSON. Put this in a new
   `hevy_parser.py` with its own unit tests.
4. **Progress logging**: store the parsed per-exercise bests in a new SQLite
   table so overload decisions can reference history, not just the last session.
5. **Telegram formatting**: optionally split long plans into multiple messages
   and add basic MarkdownV2 escaping if `parse_mode` is enabled.
6. **Config validation**: friendly startup check that reports all missing
   `.env` keys at once, with a one-line hint for each.
7. **README**: keep it current as features land. Do not create extra docs.

## Constraints for you, the agent

- Do not commit secrets or a real `.env`.
- Keep dependencies minimal; justify any new one.
- Prefer editing existing files over adding new modules unless a task calls for
  a new file (e.g. `hevy_parser.py`, tests).
- Validate each change (compile or run tests) before moving on.
- Ask before any destructive or irreversible action.

Start with task 1, then pause and show me the diff before continuing.
