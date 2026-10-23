import os
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from garminconnect import Garmin
from records import build_workout_record
from store import activities_path, read_jsonl, write_jsonl
from wellness import sync_wellness

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def sync_activities(client, start_str, today_str):
    logging.info(f"Checking Garmin activities from {start_str} to {today_str}...")

    try:
        activities = client.get_activities_by_date(start_str, today_str, "")
    except Exception as e:
        logging.error(f"Error fetching activities for range {start_str} to {today_str}: {e}")
        return

    if not activities:
        logging.info(f"No activities recorded from {start_str} to {today_str}.")
        return

    # Group new activities by their target year file to handle year-boundary edge cases safely
    files = {}

    for act in activities:
        act_id = act.get("activityId")
        activity_year = act.get('startTimeLocal', today_str)[:4]  # '2026', '2027', etc.
        path = activities_path(activity_year)

        if path not in files:
            records = read_jsonl(path)
            files[path] = {"records": records, "ids": {r.get("activity_id") for r in records}, "new_count": 0}
        data = files[path]

        if act_id in data["ids"]:
            logging.info(f"Activity ID {act_id} ('{act.get('activityName')}') is already present in {activity_year} backlog. Skipping.")
            continue

        activity_name = act.get('activityName', 'Workout')
        activity_type = act.get('activityType', {}).get('typeKey', 'unknown')
        logging.info(f"New activity found: '{activity_name}' ({activity_type}) | ID: {act_id}. Processing telemetry...")

        data["records"].append(build_workout_record(client, act))
        data["ids"].add(act_id)
        data["new_count"] += 1

    for path, data in files.items():
        if data["new_count"] > 0:
            data["records"].sort(key=lambda x: x["date"])
            write_jsonl(path, data["records"])
            logging.info(f"Successfully sorted and rewrote {path} with {data['new_count']} new activity(ies).")


def main():
    logging.info("Starting daily Garmin sync...")

    client = Garmin(os.getenv("GARMIN_EMAIL"), os.getenv("GARMIN_PASSWORD"))
    client.login(os.path.expanduser("~/.garminconnect"))

    # Multi-day lookback: dedup by activity_id makes the overlap harmless for activities, and
    # re-fetching wellness lets missed runs, late watch syncs and Garmin's own late updates catch up.
    lookback_days = int(os.getenv("LOOKBACK_DAYS", "7"))
    # TIMEZONE from .env (e.g. "America/New_York"); falls back to the machine's local timezone
    tz_name = os.getenv("TIMEZONE")
    today = datetime.now(ZoneInfo(tz_name) if tz_name else None).date()
    start = today - timedelta(days=lookback_days)

    sync_activities(client, start.isoformat(), today.isoformat())

    # Wellness stops at yesterday: today's stats aren't complete yet when this runs at 4 AM
    sync_wellness(client, start, today - timedelta(days=1))


if __name__ == "__main__":
    main()
