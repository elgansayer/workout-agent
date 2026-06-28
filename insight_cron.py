import argparse
import json
import logging
import sys
from datetime import date, timedelta
from typing import Any

import google.generativeai as genai

from config import Config, ConfigError
from database import (
    get_body_metrics,
    get_daily_logs,
    get_progress_history,
    init_db,
    save_dashboard_insight,
    save_deep_correlation,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("insight_cron")

def generate_daily_header(config: Config) -> None:
    logger.info("Generating daily insight header...")
    genai.configure(api_key=config.gemini_api_key)
    model = genai.GenerativeModel(config.gemini_model)

    # Fetch last 7 days of data
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    
    metrics = [m for m in get_body_metrics(limit=14, db_path=config.database_path) if m["date"] >= cutoff]
    logs = [log for log in get_daily_logs(limit=14, db_path=config.database_path) if log["date"] >= cutoff]

    data = {
        "metrics": metrics,
        "logs": logs
    }

    prompt = f"""You are a high-performance strength coach. Analyze the following JSON representing the user's training and recovery data for the last 7 days.

{json.dumps(data, indent=2)}

Provide a 3-bullet point executive summary for a dashboard header:
1. Current fatigue state (Green/Yellow/Red) based on training volume vs. sleep/recovery.
2. The most critical "wins" or "stalls" in the current training block.
3. One actionable adjustment for today's session.

Keep it brutally concise. Output ONLY valid JSON in this exact format, with no markdown code blocks:
{{"fatigue": "string", "wins_stalls": "string", "advice": "string"}}"""

    try:
        response = model.generate_content(prompt)
        text = (response.text or "").strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        # Validate JSON
        parsed = json.loads(text)
        if "fatigue" in parsed and "wins_stalls" in parsed and "advice" in parsed:
            save_dashboard_insight(json.dumps(parsed), db_path=config.database_path)
            logger.info("Daily insight generated successfully.")
        else:
            logger.error("Invalid JSON structure returned: %s", text)
    except Exception as e:
        logger.error("Failed to generate daily insight: %s", e)

def generate_weekly_correlations(config: Config) -> None:
    logger.info("Generating weekly deep correlations...")
    genai.configure(api_key=config.gemini_api_key)
    model = genai.GenerativeModel(config.gemini_model)

    # Fetch 60-day trailing window
    cutoff = (date.today() - timedelta(days=60)).isoformat()
    
    metrics = [m for m in get_body_metrics(limit=120, db_path=config.database_path) if m["date"] >= cutoff]
    logs = [log for log in get_daily_logs(limit=120, db_path=config.database_path) if log["date"] >= cutoff]
    
    # Also fetch training history for the last 60 days
    all_history = get_progress_history(limit_per_exercise=60, db_path=config.database_path)
    filtered_history = {}
    for ex, sets in all_history.items():
        recent = [s for s in sets if s["date"] >= cutoff]
        if recent:
            filtered_history[ex] = recent

    data = {
        "body_metrics": metrics,
        "daily_logs": logs,
        "exercise_history": filtered_history
    }

    prompt = f"""You are an elite data-driven strength coach and analyst. 
You are analyzing a 60-day trailing window of the user's training data, sleep/recovery metrics, and daily lifestyle logs.

Your goal is to hunt for invisible bottlenecks. For example, you might identify that weighted pull-up progression consistently stalls when the user has had poor recovery two nights prior, or that high-volume leg days negatively impact sleep.

Here is the data:
{json.dumps(data, indent=2)}

Analyze this data and produce a "Deep Correlation Engine" report. 
Highlight hidden correlations, potential burnout indicators, and specific tactical recommendations.
Use Markdown format. Output the Markdown report directly.
"""

    try:
        response = model.generate_content(prompt)
        text = (response.text or "").strip()
        if text:
            save_deep_correlation(text, db_path=config.database_path)
            logger.info("Weekly deep correlation generated successfully.")
    except Exception as e:
        logger.error("Failed to generate weekly deep correlation: %s", e)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--daily", action="store_true", help="Generate daily insight header")
    parser.add_argument("--weekly", action="store_true", help="Generate weekly deep correlations")
    args = parser.parse_args()

    try:
        config = Config.load()
        init_db(config.database_path)
    except ConfigError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    if args.daily:
        generate_daily_header(config)
    
    if args.weekly:
        generate_weekly_correlations(config)

if __name__ == "__main__":
    main()
