import os
import time
import logging
from datetime import date, timedelta
from dotenv import load_dotenv
from garminconnect import Garmin
from records import build_workout_record
from store import activities_path, write_jsonl
from wellness import sync_wellness

# Load environment variables
load_dotenv()

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

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

    output_path = activities_path(current_year)

    records = []
    
    # 3. Process each activity found
    for idx, act in enumerate(ytd_activities, start=1):
        act_id = act.get("activityId")
        activity_name = act.get('activityName', 'Workout')
        activity_type = act.get('activityType', {}).get('typeKey', 'unknown')
        start_date = act.get('startTimeLocal', 'Unknown Date').split(' ')[0]

        logging.info(f"[{idx}/{len(ytd_activities)}] Parsing: '{activity_name}' on {start_date} ({activity_type}) | ID: {act_id}")

        workout_record = build_workout_record(client, act)
        records.append(workout_record)

    # 5. Write all records line-by-line into JSONL
    records.sort(key=lambda x: x["date"])
    write_jsonl(output_path, records)
    logging.info(f"YTD activity backlog saved to: {output_path}")

    # 6. Daily wellness for every day of the year so far (rest days included), up to yesterday
    logging.info(f"Fetching daily wellness for {current_year}...")
    sync_wellness(client, date(current_year, 1, 1), date.today() - timedelta(days=1))

    logging.info("YTD Backlog complete!")

if __name__ == "__main__":
    fetch_ytd_backlog()