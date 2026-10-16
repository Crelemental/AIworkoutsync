import os
import json
import time
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from garminconnect import Garmin

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def safe_round(val, decimals=2):
    try:
        return round(float(val), decimals)
    except (TypeError, ValueError):
        return val

def sync_daily_activity():
    logging.info("Starting daily Garmin sync check...")
    
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    token_dir = os.path.expanduser("~/.garminconnect")

    client = Garmin(email, password)
    client.login(token_dir)

    # Local timezone date enforcement with a multi-day lookback window. Dedup by activity_id
    # makes the overlap harmless, and it lets missed runs or late watch syncs catch up.
    lookback_days = int(os.getenv("LOOKBACK_DAYS", "14"))
    # TIMEZONE from .env (e.g. "America/New_York"); falls back to the machine's local timezone
    tz_name = os.getenv("TIMEZONE")
    tz = ZoneInfo(tz_name) if tz_name else None
    now_local = datetime.now(tz)
    today_str = now_local.date().isoformat()
    start_str = (now_local.date() - timedelta(days=lookback_days)).isoformat()

    logging.info(f"Checking Garmin activities from {start_str} to {today_str} ({lookback_days}-day lookback)...")

    try:
        activities = client.get_activities_by_date(start_str, today_str, "")
    except Exception as e:
        logging.error(f"Error fetching activities for range {start_str} to {today_str}: {e}")
        return

    if not activities:
        logging.info(f"No activities recorded from {start_str} to {today_str}.")
        return

    # Group new activities by their target year file to handle year-boundary edge cases safely
    activities_by_year = {}

    for act in activities:
        act_id = act.get("activityId")
        start_date = act.get('startTimeLocal', today_str).split(' ')[0]
        activity_year = start_date.split('-')[0]  # Extracts '2026', '2027', etc.
        
        output_path = f"/root/fitness-sync/output/{activity_year}-YTD-backlog.jsonl"
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # 1. Load existing records for this specific year file into memory
        existing_records = []
        existing_activity_ids = set()
        
        if os.path.exists(output_path):
            with open(output_path, "r") as f:
                for line in f:
                    try:
                        record = json.loads(line.strip())
                        if "activity_id" in record:
                            existing_activity_ids.add(record["activity_id"])
                            existing_records.append(record)
                    except json.JSONDecodeError:
                        continue

        if act_id in existing_activity_ids:
            logging.info(f"Activity ID {act_id} ('{act.get('activityName')}') is already present in {activity_year} backlog. Skipping.")
            continue

        activity_name = act.get('activityName', 'Workout')
        activity_type = act.get('activityType', {}).get('typeKey', 'unknown')

        logging.info(f"New activity found: '{activity_name}' ({activity_type}) | ID: {act_id}. Processing telemetry...")

        workout_record = {
            "date": start_date,
            "activity_id": act_id,
            "name": activity_name,
            "type": activity_type,
            "duration_mins": safe_round(act.get('duration', 0) / 60),
            "calories": safe_round(act.get('calories', 0)),
            "heart_rate": {
                "avg": safe_round(act.get('averageHR', 'N/A')),
                "max": safe_round(act.get('maxHR', 'N/A'))
            },
            "training_effect": {
                "aerobic": safe_round(act.get('aerobicTrainingEffect', 'N/A')),
                "anaerobic": safe_round(act.get('anaerobicTrainingEffect', 'N/A')),
                "exercise_load": safe_round(act.get('activityTrainingLoad', 'N/A'))
            },
            "metrics": {},
            "laps": []
        }

        # Sport-specific metrics
        if "run" in activity_type:
            workout_record["metrics"] = {
                "distance_miles": safe_round(act.get('distance', 0) / 1609.34),
                "cadence_spm": safe_round(act.get('averageRunningCadenceInStepsPerMinute', 'N/A')),
                "stride_length_m": safe_round(act.get('avgStrideLength', 'N/A')),
                "ground_contact_time_ms": safe_round(act.get('avgGroundContactTime', 'N/A')),
                "vertical_oscillation_cm": safe_round(act.get('avgVerticalOscillation', 'N/A'))
            }
        elif "cycle" in activity_type or "bike" in activity_type:
            speed_ms = act.get('averageSpeed', 0)
            workout_record["metrics"] = {
                "distance_miles": safe_round(act.get('distance', 0) / 1609.34),
                "avg_speed_mph": safe_round(speed_ms * 2.23694),
                "cadence_rpm": safe_round(act.get('averageBikingCadenceInRpm', 'N/A'))
            }

        avg_power = act.get('averagePower')
        if avg_power:
            workout_record["metrics"]["power_watts"] = {
                "avg": safe_round(avg_power),
                "max": safe_round(act.get('maxPower', 'N/A'))
            }

        # Fetch Laps
        time.sleep(1.0)
        try:
            raw_splits = client.get_activity_splits(act_id)
            lap_splits = []
            if isinstance(raw_splits, dict):
                lap_splits = raw_splits.get('lapDTOs') or raw_splits.get('activitySplits') or raw_splits.get('lapSplits') or []
            elif isinstance(raw_splits, list):
                lap_splits = raw_splits

            if not lap_splits:
                summaries = client.get_activity_split_summaries(act_id)
                if isinstance(summaries, list):
                    lap_splits = summaries

            for lap_idx, split in enumerate(lap_splits, start=1):
                speed_ms = split.get('speed', split.get('averageSpeed', 0))
                pace_str = "N/A"
                if "run" in activity_type and speed_ms > 0:
                    sec_per_mile = 1609.34 / speed_ms
                    p_m = int(sec_per_mile // 60)
                    p_s = int(sec_per_mile % 60)
                    pace_str = f"{p_m}:{p_s:02d}/mi"
                elif ("cycle" in activity_type or "bike" in activity_type) and speed_ms > 0:
                    pace_str = f"{safe_round(speed_ms * 2.23694)} mph"

                workout_record["laps"].append({
                    "lap": split.get('lapIndex', split.get('splitIndex', lap_idx)),
                    "distance_mi": safe_round(split.get('distance', 0) / 1609.34),
                    "duration_secs": safe_round(split.get('duration', split.get('elapsedDuration', 0))),
                    "pace_or_speed": pace_str,
                    "avg_hr": safe_round(split.get('averageHR', split.get('averageHeartRate', 'N/A'))),
                    "max_hr": safe_round(split.get('maxHR', 'N/A')),
                    "elevation_gain_ft": safe_round(split.get('elevationGain', 0))
                })
        except Exception as e:
            logging.warning(f"Could not retrieve lap data for ID {act_id}: {e}")

        # Queue for writing grouped by file path
        if output_path not in activities_by_year:
            activities_by_year[output_path] = {
                "records": existing_records,
                "new_count": 0
            }
        
        activities_by_year[output_path]["records"].append(workout_record)
        activities_by_year[output_path]["new_count"] += 1
        logging.info(f"Added new activity '{activity_name}' to queue for {activity_year}.")

    # 2. Write and sort each target year file
    for path, data in activities_by_year.items():
        if data["new_count"] > 0:
            data["records"].sort(key=lambda x: x["date"])
            # Write to a temp file and swap it in atomically so push.sh never copies a half-written file
            tmp_path = path + ".tmp"
            with open(tmp_path, "w") as f:
                for rec in data["records"]:
                    f.write(json.dumps(rec) + "\n")
            os.replace(tmp_path, path)
            logging.info(f"Successfully sorted and rewrote {path} with {data['new_count']} new activity(ies).")

if __name__ == "__main__":
    sync_daily_activity()