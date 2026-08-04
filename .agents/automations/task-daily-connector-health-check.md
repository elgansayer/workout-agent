# Daily Connector Health Check

## Objective
Make sure every connector (Hevy, Google Health, Health Connect, weather,
Telegram) degrades gracefully and stays per-user-credential-driven as the
product grows.

## Instructions
1. For each connector module (`hevy_client.py`, `google_health_client.py`,
   `health_connect.py`, `weather.py`, `telegram_notifier.py`), confirm every
   network call is wrapped so a timeout/4xx/5xx/malformed response can't
   crash the caller — catch, log, and return a sensible empty/None result,
   matching the `connector-integration` skill's failure-isolation
   requirement.
2. Check whether any connector has regressed back to reading a single global
   env var for something that should now be a per-user credential (e.g. a
   new code path accidentally reading `TELEGRAM_CHAT_ID` directly instead of
   a per-user setting, once that migration has started) — flag or fix.
3. Confirm `.env.example` still documents every connector-related env var
   currently read anywhere in the codebase, and doesn't document any that no
   longer exist (grep both directions).
4. If a genuine bug is found (e.g. an unhandled exception path), fix it and
   add a regression test in the connector's `tests/test_*.py` module.
