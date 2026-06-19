import time
import subprocess
from datetime import datetime, timedelta

def run_daily():
    print("[scheduler] Running daily insights...")
    subprocess.run(["python", "insight_cron.py", "--daily"])

def run_weekly():
    print("[scheduler] Running weekly deep correlations...")
    subprocess.run(["python", "insight_cron.py", "--weekly"])

def get_next_run(hour=6, minute=0):
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target

if __name__ == "__main__":
    # Run once on startup to ensure we have data immediately
    run_daily()
    run_weekly()
    
    while True:
        now = datetime.now()
        next_run = get_next_run()
        sleep_secs = (next_run - now).total_seconds()
        print(f"[scheduler] Sleeping {sleep_secs}s until next run at {next_run}")
        time.sleep(sleep_secs)
        
        run_daily()
        if datetime.now().weekday() == 6: # Sunday
            run_weekly()
