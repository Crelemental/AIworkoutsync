import os
import json
import time
import logging
from datetime import date
from dotenv import load_dotenv
from garminconnect import Garmin

# Load environment variables
load_dotenv()

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def safe_round(val, decimals=2):
    """Safely round numerical values to specified decimals, leaving strings untouched."""
    try:
        return round(float(val), decimals)
    except (TypeError, ValueError):
        return val

def fetch_ytd_backlog():
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    token_dir = os.path.expanduser("~/.garminconnect")

    client = None
    max_retries = 3
    backoff_factor = 5

    # 1. Authenticate with Retry & Backoff for 429 Errors
    for attempt in range(1, max_retries + 1):
        try:
            logging.info(f"Authenticating with Garmin (Attempt {attempt}/{max_retries})...")
            client = Garmin(email, password)
            client.login(token_dir)
            logging.info("Authentication successful!")
            break
        except Exception as e:
            logging.error(f"Authentication failed: {e}")
            if attempt == max_retries:
                logging.critical("Max authentication retries reached. Exiting.")
                raise
            sleep_time = backoff_factor * (2 ** (attempt - 1))
            logging.warning(f"Rate limited or error encountered. Waiting {sleep_time} seconds before retrying...")
            time.sleep(sleep_time)

    current_year = date.today().year
    logging.info(f"Fetching bulk activities for {current_year} from Garmin Connect...")

    # 2. Bulk fetch activities
    try:
        all_activities = client.get_activities(0, 1000)
    except Exception as e:
        logging.error(f"Failed to fetch bulk activities: {e}")
        return

    ytd_activities = [
        act for act in all_activities 
        if act.get('startTimeLocal', '').startswith(str(current_year))
    ]

    logging.info(f"Found {len(ytd_activities)} activities for {current_year}. Processing JSONL backlog...")

    output_path = f"/root/fitness-sync/output/{current_year}-YTD-backlog.jsonl"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    records = []
    
    # 3. Process each activity found
    for idx, act in enumerate(ytd_activities, start=1):
        act_id = act.get("activityId")
        activity_name = act.get('activityName', 'Workout')
        activity_type = act.get('activityType', {}).get('typeKey', 'unknown')
        start_date = act.get('startTimeLocal', 'Unknown Date').split(' ')[0]

        logging.info(f"[{idx}/{len(ytd_activities)}] Parsing: '{activity_name}' on {start_date} ({activity_type}) | ID: {act_id}")

        # Base structure
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

        # Sport-specific telemetry
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

        # Power
        avg_power = act.get('averagePower')
        if avg_power:
            workout_record["metrics"]["power_watts"] = {
                "avg": safe_round(avg_power),
                "max": safe_round(act.get('maxPower', 'N/A'))
            }

        # 4. Fetch Laps / Splits safely
        time.sleep(1.0)
        try:
            raw_splits = client.get_activity_splits(act_id)
            lap_splits = []
            
            if isinstance(raw_splits, dict):
                lap_splits = (
                    raw_splits.get('lapDTOs') or 
                    raw_splits.get('activitySplits') or 
                    raw_splits.get('lapSplits') or []
                )
            elif isinstance(raw_splits, list):
                lap_splits = raw_splits

            # Fallback to split summaries if primary split lookup returns empty
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

                avg_hr_val = split.get('averageHR', split.get('averageHeartRate', 'N/A'))
                max_hr_val = split.get('maxHR', split.get('maxHeartRate', 'N/A'))

                workout_record["laps"].append({
                    "lap": split.get('lapIndex', split.get('splitIndex', lap_idx)),
                    "distance_mi": safe_round(split.get('distance', 0) / 1609.34),
                    "duration_secs": safe_round(split.get('duration', split.get('elapsedDuration', 0))),
                    "pace_or_speed": pace_str,
                    "avg_hr": safe_round(avg_hr_val),
                    "max_hr": safe_round(max_hr_val),
                    "elevation_gain_ft": safe_round(split.get('elevationGain', 0))
                })

            logging.info(f"   ↳ Retrived {len(workout_record['laps'])} lap split(s) for ID {act_id}")

        except Exception as e:
            logging.warning(f"   ↳ Could not retrieve lap data for ID {act_id}: {e}")

        records.append(workout_record)

    # 5. Write all records line-by-line into JSONL
    with open(output_path, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")

    logging.info(f"YTD Backlog complete! Output saved to: {output_path}")

if __name__ == "__main__":
    fetch_ytd_backlog()