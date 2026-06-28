import time
import subprocess
import logging
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("insight_scheduler")

def run_daily():
    logger.info("Running daily insights...")
    try:
        subprocess.run(["python", "insight_cron.py", "--daily"], check=True)
    except subprocess.CalledProcessError as e:
        logger.error("Daily insights failed with exit code %s", e.returncode)

def run_weekly():
    logger.info("Running weekly deep correlations...")
    try:
        subprocess.run(["python", "insight_cron.py", "--weekly"], check=True)
    except subprocess.CalledProcessError as e:
        logger.error("Weekly insights failed with exit code %s", e.returncode)

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
        sleep_secs = max(0, (next_run - now).total_seconds())
        logger.info("Sleeping %.0fs until next run at %s", sleep_secs, next_run.strftime("%Y-%m-%d %H:%M:%S"))
        time.sleep(sleep_secs)
        
        run_daily()
        if datetime.now().weekday() == 6: # Sunday
            run_weekly()
