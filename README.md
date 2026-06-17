# Autonomous Workout Agent

A private, local Python agent that acts as an elite hypertrophy / stage-prep
bodybuilding coach. Every morning it pulls your latest workout from the
[Hevy API](https://api.hevyapp.com/docs/), reads your recovery metrics from
Google Health Connect (via an exported JSON file), asks Google Gemini to apply
progressive overload, then sends tomorrow's exact routine plus one daily
improvement tip to your phone via Telegram.

Built as a small, focused script rather than a heavyweight generalist agent
framework. It does one thing well: read your data, reason about it, message you.

---

## Why a custom agent (not OpenClaw / generalist frameworks)

- **Precision** – one job: parse Hevy JSON against a 6-day split and message you.
- **Privacy** – your health data stays on your machine and is only sent to Gemini.
- **Low complexity** – a script on a cron job. Set it and forget it.
- **Safety** – no broad shell/file access, no sandbox to harden.

---

## The trainee & constraints (the agent's brief)

The coach persona must always respect these facts about the athlete (Elgan):

- Goal: **Greek-god physique** – broad shoulders, wide lats, V-taper, narrow
  waist, defined arms – and **fat loss** via a caloric deficit.
- Long history with the **Arnold split**; wants to love the gym again.
- **Cannot lift heavy** any more (joints). Train for hypertrophy, not 1RM.
- **3-second negative (eccentric)** on every rep. Momentum is banned.
- **Rep ranges 10–20**, sets taken close to failure.
- **Pre-exhaust** with isolation before compound to protect joints.
- **No Bulgarian split squats** (bad toes) → use flat-foot **Leg Press**.
- **No stomach vacuums** → train abs for mass with progressive overload.
- Also does **Thai boxing** and **bouldering**, so shoulders/elbows need care
  (favour lateral/rear-delt isolation over heavy overhead pressing).
- Protein ~**2 g/kg** bodyweight to preserve muscle in a deficit.
- **British English** spelling (e.g. "programme"). **Never use the em dash.**

---

## The perfected 6-day split

> Lives in [program.py](program.py) as structured data so the agent can reason
> over it and progress it.

**Day 1 & 4: Chest & Back (V-taper)**
- Incline Dumbbell Flyes — 4 × 12–15 (pre-exhaust)
- Incline Smith Machine Press — 3 × 10–12
- Chest-Supported T-Bar Rows — 3 × 10–12
- Wide-Grip Lat Pulldowns — 4 × 12
- Straight-Arm Cable Pull-Downs — 3 × 15

**Day 2 & 5: Shoulders & Arms (3D delts, peaks)**
- Cable Lateral Raises — 5 × 15–20
- Reverse Pec Deck Flyes — 4 × 15
- Incline Dumbbell Curls — 4 × 12
- Tricep Overhead Cable Extensions — 4 × 12–15
- Reverse-Grip Cable Curls — 3 × 15

**Day 3 & 6: Legs & Abs**
- Lying Leg Curls — 4 × 12 (hamstring isolation)
- Leg Extensions — 4 × 15
- Leg Press (feet flat) — 3 × 10–12
- Romanian Deadlifts (dumbbells) — 4 × 12
- Leg Press Calf Raises — 4 × 15–20
- Hanging Leg Raises — 4 × 12–15
- Kneeling Cable Crunches — 4 × 10–12

---

## Architecture

```
workout_agent/
├── main.py              # Orchestrates the nightly run
├── config.py            # Loads secrets from environment / .env
├── database.py          # SQLite: programme state + workout history
├── program.py           # The perfected 6-day split as structured data
├── hevy_client.py       # Pulls latest workout from the Hevy API
├── health_connect.py    # Reads sleep/weight JSON exported from Health Connect
├── gemini_engine.py     # Asks Gemini to apply progressive overload
├── telegram_notifier.py # Sends the message to your phone
├── requirements.txt
├── .env.example         # Copy to .env and fill in (never committed)
└── .gitignore
```

Data flow each morning:

```
Hevy API ─┐
          ├─► gemini_engine ─► Telegram (your phone)
Health ───┤        ▲
Connect   │        │
SQLite ───┘  (current day + history)
```

---

## Setup

1. **Install dependencies**

   ```bash
   cd workout_agent
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure secrets** – copy the template and fill it in:

   ```bash
   cp .env.example .env
   # then edit .env
   ```

   | Variable             | Where to get it                                    |
   | -------------------- | -------------------------------------------------- |
   | `HEVY_API_KEY`       | Hevy web dashboard → Settings → API                |
   | `GEMINI_API_KEY`     | https://aistudio.google.com/app/apikey             |
   | `TELEGRAM_BOT_TOKEN` | Talk to @BotFather on Telegram                     |
   | `TELEGRAM_CHAT_ID`   | Talk to @userinfobot, or see note below            |
   | `GEMINI_MODEL`       | Optional, defaults to `gemini-2.0-flash`           |

   To find your `TELEGRAM_CHAT_ID`: message your new bot once, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `result[].message.chat.id`.

3. **Health Connect (optional, Android)** – Health Connect data is locked to
   your phone. Use **Health Sync** or **Tasker** to export a small daily JSON to
   a folder synced to this machine, then point `HEALTH_CONNECT_FILE` at it. The
   agent reads sleep hours and bodyweight to adjust recovery advice. Expected
   shape:

   ```json
   { "date": "2026-06-17", "sleep_hours": 7.5, "weight_kg": 82.0, "resting_hr": 58 }
   ```

   If the file is missing the agent simply runs without recovery data.

4. **Run it once to test**

   ```bash
   python main.py
   ```

5. **Schedule it for 7:00 AM daily (cron)**

   ```bash
   crontab -e
   # add (adjust the absolute paths):
   0 7 * * * cd /path/to/workout_agent && /path/to/workout_agent/.venv/bin/python main.py >> agent.log 2>&1
   ```

---

## Security notes

- Secrets live in `.env` (git-ignored), never in committed source.
- All outbound HTTP calls use explicit timeouts and handle failures gracefully.
- The agent has no shell or arbitrary file access; it only reads the one
  Health Connect file path you configure.

---

## Hevy export fallback (no API key)

If you would rather not use the API, export your history from Hevy
(Profile → Settings → Export & Import Data → Export Data) and paste recent rows
in; the same `gemini_engine` prompt works on pasted CSV/JSON text.
