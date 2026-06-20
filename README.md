# Autonomous Workout Agent

A private, local Python agent that acts as an elite hypertrophy / stage-prep
bodybuilding coach. Every morning it works out what you should train today from
the real calendar day, pulls your latest workout from the
[Hevy API](https://api.hevyapp.com/docs/), reads your recovery metrics from
Google Health Connect (via an exported JSON file), asks Google Gemini to apply
progressive overload, then sends today's exact routine plus one daily
improvement tip to your phone via Telegram. On Sundays it sends a short rest and
recovery message instead.

Built as a small, focused script rather than a heavyweight generalist agent
framework. It does one thing well: read your data, reason about it, message you.

<img width="795" height="1770" alt="image" src="https://github.com/user-attachments/assets/de803041-7959-4230-93b3-bda786d1a406" />
<img width="795" height="1770" alt="image" src="https://github.com/user-attachments/assets/cf1e2e9b-37ac-4b9e-81eb-c0ee4ee00d4b" />
<img width="795" height="1770" alt="image" src="https://github.com/user-attachments/assets/75bff5d6-26f0-4278-8ea0-bd88b9d2a7c6" />
<img width="795" height="1770" alt="image" src="https://github.com/user-attachments/assets/6beffcbb-aa10-4dc7-b561-486b5a43dc76" />
<img width="795" height="1770" alt="image" src="https://github.com/user-attachments/assets/c345415d-a7bc-4031-a420-22c1a3bf219e" />
<img width="795" height="1770" alt="image" src="https://github.com/user-attachments/assets/0d3a355a-2cc2-4c5b-85aa-7e34cf985503" />
<img width="795" height="1770" alt="image" src="https://github.com/user-attachments/assets/217e8556-21df-471e-93a6-bb82e31d316a" />
<img width="795" height="1770" alt="image" src="https://github.com/user-attachments/assets/e68f0f1a-a508-4ca7-a980-8fc652ffd3f9" />

---

## Why a custom agent (not OpenClaw / generalist frameworks)

- **Precision** – one job: parse Hevy JSON against a 6-day split and message you.
- **Privacy** – your health data stays on your machine and is only sent to Gemini.
- **Low complexity** – a script on a cron job. Set it and forget it.
- **Safety** – no broad shell/file access, no sandbox to harden.

---

## AI-Native "Insight-First" Dashboard & Correlation Engine

The dashboard acts as an intelligent, reactive interface that treats your SQLite database as a knowledge graph, powered by **Gemini 1.5 Pro**:

- **Coach's Status Header**: The top-level dashboard metrics are replaced with a natural language executive summary generated every morning. It checks your fatigue state (volume vs. sleep), highlights block wins/stalls, and gives an actionable adjustment for today.
- **Deep Correlation Engine**: A weekly background job that hunts for invisible bottlenecks across a 60-day trailing window of your training volume, sleep metrics, and lifestyle. It flags burnout indicators or stalling patterns.
- **Explainable UI (XAI) Overlays**: Trend charts have a "Why did this happen?" toggle. The frontend queries Gemini with the specific exercise history and block goal to give a clear causal explanation, stored in the database.
- **Predictive Progressive Overload Visuals**: Projects your estimated 1RM to the end of the peaking phase and validates whether the forecast is physiologically aggressive or conservative based on recent "bad" sessions.
- **RAG-Enabled Search (Log Investigator)**: A search bar in the dashboard to ask questions like *"Why did I fail my squats in week 3?"*. The backend vectorizes logs and retrieves relevant session context via streaming LLM generation.

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

**Weekly schedule** (driven by the real calendar day):

| Day | Focus |
| --------- | ----------------------------- |
| Mon / Thu | Chest & Back (V-taper)        |
| Tue / Fri | Shoulders & Arms (3D delts)   |
| Wed / Sat | Legs & Abs                    |
| Sun       | Rest & Recovery               |

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
├── main.py              # Orchestrates the morning run
├── config.py            # Loads secrets from environment / .env
├── database.py          # SQLite: workout history + per-exercise progress
├── program.py           # The perfected 6-day split as structured data
├── hevy_client.py       # Pulls latest workout from the Hevy API
├── hevy_parser.py       # Distils the Hevy payload into a compact summary
├── hevy_sync.py         # Builds/updates the sessions as Hevy routines
├── health_connect.py    # Reads sleep/weight JSON exported from Health Connect
├── google_health_client.py # Auto-syncs weight/body fat from Google Health (Eufy scale)
├── google_health_auth.py   # One-time OAuth helper to obtain the first refresh token
├── gemini_engine.py     # Asks Gemini to apply progressive overload
├── checkin.py           # Periodic planned-vs-actual check-in engine
├── lifestyle.py         # Daily lifestyle pillars (nutrition/cardio/recovery)
├── telegram_notifier.py # Sends the message to your phone
├── webapp/              # FastAPI + Jinja2 dashboard (server-rendered SVG charts)
├── tests/               # pytest suite (no network access)
├── requirements.txt
├── requirements-web.txt # Extra deps for the web dashboard
├── .env.example         # Copy to .env and fill in (never committed)
└── .gitignore
```

Data flow each morning:

```
Hevy API ─┐  parsed by hevy_parser
          ├─► gemini_engine ─► Telegram (your phone)
Health ───┤        ▲
Connect   │        │
SQLite ───┘   (workout history + per-exercise bests)
```

The day's training focus is derived from the real calendar day (Monday to
Saturday follow the 6-day split; Sunday is a rest day), so a missed run never
knocks the schedule out of sync. Each session's top sets are parsed from Hevy
and stored, so progressive overload references your real history rather than
just the last raw payload.

---

## Routine check-ins (every 4-6 weeks)

The agent runs a **check-in** roughly every 4-6 weeks. It fires once you have
logged enough sessions to reach the planned workout count, with a calendar cap
so it never waits longer than six weeks (or falls back to four weeks if no Hevy
count is available). A check-in:

- reads your logged Hevy history since the last check-in,
- compares **planned vs actual** progression for each main lift,
- flags lifts that are progressing well and lifts that have **stalled**
  (three or more sessions at the top of the rep range with no load increase),
- asks Gemini to summarise the findings and **auto-applies** them, then
- sends you a Telegram **check-in digest** so you always know what changed.

It is numbered ("Check-in 1", "Check-in 2", ...) and stored in SQLite. Set
`CHECKIN_ENABLED=0` to turn the feature off.

### Change notifications

Whenever the agent refreshes your Hevy routines, the day's Telegram message ends
with a short footer listing exactly which routines were **created or updated**,
so programme changes are never silent.

---

## Daily lifestyle pillars (the 90% outside the gym)

The gym is only part of the battle. Every daily message now ends with a
**"what to do today"** lifestyle block, derived from the day's training focus and
logged alongside the plan. It names today's session and covers the supporting
pillars ([lifestyle.py](lifestyle.py)):

1. **Strategic nutrition (carb cycling)** – protein is held static at
   **2.2 g/kg**; carbohydrates cycle with load. Heavy deadlift/back days (Day 1 &
   4) run **high carb** (about 70% around the workout), leg days (Day 3 & 6) run
   **moderate**, and the lighter upper days (Day 2 & 5) and rest days run **low
   carb / higher healthy fats**. If a bodyweight reading is available, it prints
   your exact protein target in grams.
2. **Joint-friendly cardio and NEAT** – a daily **10-12k step** target, plus
   **20-30 min of Zone 2** (stationary bike or swim) on the lighter days and rest
   days. Heavy pull and leg days skip steady-state to protect recovery. No
   stair-master or running (bad toes).
3. **CNS recovery** – **8 h sleep** minimum, Omega-3, and Magnesium Glycinate
   before bed.

Set `LIFESTYLE_ENABLED=0` to turn the block off.

### Everything is logged

Each run records the full plan and lifestyle guidance it issued to a `daily_log`
table (one row per day), and any **body-composition reading** (weight, body fat,
muscle, resting heart rate) to a `body_metrics` table. The web dashboard plots
your weight and body-fat trend from this log.

### Reading your Eufy Life scale (weight and body fat)

Eufy Life has no public API, but the scale already syncs body weight and body fat
to the cloud, so the agent can read them automatically with no manual export. Two
options:

**Automatic, cloud-to-cloud (recommended): Google Health.** Eufy Life syncs to
Fitbit / Google Health, which exposes a proper Web API the agent polls every run.

> **Note: this replaces the legacy Fitbit Web API, which is deprecated in
> September 2026.** New applications can no longer be registered on the old
> `dev.fitbit.com` form; the agent now uses the Google Health API instead.

1. In the **Eufy Life** app, enable syncing to **Fitbit** (the data flows through
   to Google Health).
2. Create an OAuth client at the
   [Google Health API developer site](https://developers.google.com/health/setup):
   choose **Web Server**, add the `health_metrics_and_measurements.readonly`
   scope, and add your Google account under **Test users**.
3. Authorise it once to obtain a refresh token. The quickest way is the bundled
   helper: set `GOOGLE_HEALTH_CLIENT_ID` and `GOOGLE_HEALTH_CLIENT_SECRET` in
   `.env`, add `http://localhost:8080/` as an Authorized redirect URI, then run
   `python google_health_auth.py`. It opens the consent page, captures the
   redirect, exchanges it for tokens, stores the refresh token in the database,
   and prints the `GOOGLE_HEALTH_REFRESH_TOKEN` line to paste into `.env`.

That is it: the agent pulls your latest weight and body fat from Google Health on
every run, refreshes the access token for you, logs the body composition, and
uses the weight to compute your daily protein target. Nothing is exported by
hand.

> Tip: while your OAuth consent screen is still in **Testing** status, Google
> issues refresh tokens that expire after 7 days. Publish the app (still just for
> yourself) to get a long-lived refresh token.

**Alternative: a synced file.** If you prefer not to use Google Health, have the
Eufy Life app sync to **Google Health Connect**, then use **Health Sync** or
**Tasker** to drop a small daily JSON into a folder synced to this machine
(Syncthing, Nextcloud, etc.) and point `HEALTH_CONNECT_FILE` at it:

```json
{ "date": "2026-06-17", "sleep_hours": 7.5, "weight_kg": 82.0,
  "body_fat_pct": 14.2, "muscle_pct": 47.5, "resting_hr": 58 }
```

This still runs automatically once the phone-side automation is set up, and is
the route to also bring in sleep and resting heart rate.

---

## Internal web dashboard

A **FastAPI + Jinja2** web app turns the agent's database into a rich, read-only
control centre. Every chart is **server-rendered SVG** (no JavaScript, no chart
library, no external calls), so pages load instantly and work fully offline
behind a reverse proxy. All motivation is automated: the dashboard shows a daily
hype line chosen from the date, with no buttons to press.

| Route        | Shows                                                          |
| ------------ | -------------------------------------------------------------- |
| `/`          | Today's block, session, cycle/block rings, streak, body sparklines, consistency calendar, daily quote |
| `/progress`  | Server-rendered SVG line charts per lift and body composition, with estimated 1RM badges |
| `/stats`     | Headline totals, training-split and muscle-group donuts, strength projections, DOTS/relative-strength trend, session-load bars, all-time personal records |
| `/plan`      | The full 12-week periodisation, the 6-day split for the current block, and coaching rules |
| `/history`   | A training-consistency calendar heatmap and the daily plan log |
| `/checkins`  | The full history of routine check-in digests                   |

Run it with Docker alongside the agent (it shares the same SQLite volume):

```bash
docker compose up -d web
```

Then open `http://<host-ip>:8770` from any device on your network. To run it
directly instead:

```bash
pip install -r requirements.txt -r requirements-web.txt
uvicorn webapp.app:app --host 0.0.0.0 --port 8000
```

The dashboard is a Progressive Web App: open it in a mobile browser and use
"Add to Home Screen" to install it as a standalone app. A service worker caches
the app shell so it opens instantly and survives brief connection drops.

### Hosting behind a reverse proxy (e.g. a public domain)

The app is read-only and has no login, so it sits cleanly behind Apache, nginx,
or Caddy. Point the proxy at the container's published port. Example Apache
virtual host mapping `gym.example.com` to the dashboard:

```apache
<VirtualHost *:443>
    ServerName gym.example.com
    ProxyPreserveHost On
    ProxyPass        / http://127.0.0.1:8770/
    ProxyPassReverse / http://127.0.0.1:8770/
    # ... your TLS configuration ...
</VirtualHost>
```

Because there is no authentication, only expose data you are happy to be public,
or add HTTP basic auth at the proxy if you want to gate it.

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
   | `HEVY_API_KEY`       | Optional. Hevy web dashboard, Settings, API        |
   | `GEMINI_API_KEY`     | https://aistudio.google.com/app/apikey             |
   | `TELEGRAM_BOT_TOKEN` | Talk to @BotFather on Telegram                     |
   | `TELEGRAM_CHAT_ID`   | Talk to @userinfobot, or see note below            |
   | `GEMINI_MODEL`       | Optional, defaults to `gemini-2.5-flash`           |
   | `TELEGRAM_PARSE_MODE`| Optional, blank for plain text or `MarkdownV2`     |
   | `CHECKIN_ENABLED`    | Optional, `1` (default) to run periodic check-ins  |
   | `LIFESTYLE_ENABLED`  | Optional, `1` (default) to append daily lifestyle  |

   If any required key is missing, the agent reports them all at once on
   startup, each with a one-line hint, rather than failing one at a time.

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

   Use `--preview` for a dry run that prints today's plan to stdout without
   sending anything to Telegram:

   ```bash
   python main.py --preview
   ```

5. **Populate AI Insights & Test Locally**
   
   The easiest way to test everything locally is using Docker. Run the included start script, which spins up the database, the backend agent, and the frontend web app, and forces an initial data population run so you can see the AI insights immediately.
   
   ```bash
   ./start.sh
   ```
   
   Open `http://localhost:8088` (or the port defined in your `.env`) in your browser. The background schedulers inside the container will automatically take over running daily/weekly tasks.

---

## Run it with Docker (no Python needed)

If you would rather not touch Python at all, run it as a self-scheduling
container. It wakes at `RUN_AT` (default `00:00,05:00`, in your `TZ`) and
messages you,
then sleeps until the next day. The SQLite data lives in a named volume so your
history survives rebuilds.

1. **Fill in your keys** (still needed once):

   ```bash
   cp .env.example .env
   # then edit .env
   ```

2. **Start it** (builds on first run, then stays up and messages you daily):

   ```bash
   docker compose up -d --build
   docker compose logs -f          # watch it; shows the next scheduled run
   ```

3. **Preview now without sending anything**:

   ```bash
   docker compose run --rm -e MODE=preview agent
   ```

4. **Send one real message immediately**:

   ```bash
   docker compose run --rm -e MODE=once agent
   ```

Adjust the time and timezone in [docker-compose.yml](docker-compose.yml) via
`RUN_AT` and `TZ`. To feed in Health Connect data, uncomment the `/health`
volume mount and set `HEALTH_CONNECT_FILE=/health/recovery.json` in `.env`.
`docker compose down` stops it; your data in the `agent-data` volume remains.

5. **Start the internal web dashboard** (optional, shares the same data volume):

   ```bash
   docker compose up -d web
   ```

   It listens on `http://<host-ip>:8770`. Host it on a Proxmox LXC and reach it
   from any device on your LAN. Keep it on a trusted network: there is no auth.

---

## Run it fully automated on a VPS with Portainer

Pre-built images are published to the GitHub Container Registry (GHCR) by the
[build-images workflow](.github/workflows/build-images.yml) on every push to
`main`, so the VPS never builds anything — it just pulls them:

- `ghcr.io/elgansayer/workout-agent:latest` — the daily agent
- `ghcr.io/elgansayer/workout-agent-web:latest` — the read-only dashboard

The [Portainer stack](docker-compose.portainer.yml) runs **both**: the agent
wakes at `RUN_AT` every day (default midnight and 5am in your `TZ`), builds the plan, syncs
Hevy routines and Google Health body composition, and messages you on Telegram;
the dashboard serves a live view of the same data on port `8770`.

**Credentials live only in Portainer, never in git** — the compose references
variable names (`${...}`) and you supply the values in the stack's
**Environment variables** panel.

### 1. (Optional) Google Health body-composition sync

The easiest way is the **"Connect Google Health" button** on the dashboard's
**Settings** page: after the stack is up, set `GOOGLE_HEALTH_CLIENT_ID`,
`GOOGLE_HEALTH_CLIENT_SECRET` and `GOOGLE_HEALTH_REDIRECT_URI` (your dashboard's
public `…/google-health/callback` URL, also registered on the OAuth client),
open Settings, click Connect, approve once — the token is stored in the database
and the agent uses it automatically. No laptop or manual token needed.

Prefer the command line? Generate the refresh token once on your laptop instead
and paste it in as `GOOGLE_HEALTH_REFRESH_TOKEN`:

```bash
# on your laptop, in a checkout of this repo
export GOOGLE_HEALTH_CLIENT_ID=...        # from https://developers.google.com/health/setup
export GOOGLE_HEALTH_CLIENT_SECRET=...    # OAuth client, redirect URI http://localhost:8080/
python google_health_auth.py              # approve in the browser; it prints the refresh token
```

Skip this whole step if you don't use a smart scale — the agent still works
without it.

### 2. Deploy the stack in Portainer

1. **Stacks → Add stack**, then either pick **Repository** and point it at
   [docker-compose.portainer.yml](docker-compose.portainer.yml), or choose
   **Web editor** and paste that file's contents.
2. Under **Environment variables**, add your credentials:

   | Variable | Required | Notes |
   |---|---|---|
   | `GEMINI_API_KEY` | ✅ | https://aistudio.google.com/app/apikey |
   | `TELEGRAM_BOT_TOKEN` | ✅ | from @BotFather |
   | `TELEGRAM_CHAT_ID` | ✅ | your chat id |
   | `HEVY_API_KEY` | optional | reference your last logged session |
   | `GOOGLE_HEALTH_CLIENT_ID` | optional | smart-scale sync |
   | `GOOGLE_HEALTH_CLIENT_SECRET` | optional | smart-scale sync |
   | `GOOGLE_HEALTH_REDIRECT_URI` | optional | dashboard `…/google-health/callback` URL for the Connect button |
   | `GOOGLE_HEALTH_REFRESH_TOKEN` | optional | only if linking via the CLI instead of the button |
   | `RUN_AT`, `TZ`, `WEB_PORT` | optional | defaults `00:00,05:00`, `Europe/London`, `8770` |

3. **Deploy the stack.** It now runs every day on its own. The dashboard is at
   `http://<vps-ip>:8770` — keep it behind a reverse proxy / firewall, there is
   no auth.

> Want the agent to message you immediately to confirm it works? Temporarily set
> `MODE=once` as an env var and redeploy, then set it back to `schedule`.

---

## Testing

The suite is pure and never touches the network (it uses temporary databases
and files):

```bash
pip install -r requirements.txt
pytest
```

It covers the database (seed, advance/wrap, progress logging), the programme and
weekday scheduling, the Health Connect reader, the Hevy parser, and the Telegram
splitting/escaping helpers.

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
