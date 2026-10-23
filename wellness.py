"""Builds one JSONL line per calendar day (rest days included): sleep, HRV, heart rate, Body Battery,
stress, daily movement, training readiness and training status. Shared by garminsync.py and garminsync_backlog.py."""
import time
import logging
from datetime import datetime, timedelta, timezone

from records import safe_round, _try, METERS_PER_MILE
from store import activities_path, wellness_path, read_jsonl, write_jsonl


def _phrase(code):
    """Garmin feedback codes like "PRODUCTIVE_5" or "AEROBIC_HIGH_SHORTAGE" -> "Productive" / "Aerobic High Shortage"."""
    if not code or code == "NONE":
        return None
    parts = code.split("_")
    if parts[-1].isdigit():
        parts = parts[:-1]
    return " ".join(parts).title()


def _mins(secs):
    return int(round(secs / 60)) if isinstance(secs, (int, float)) else None


def _clock(local_epoch_ms):
    """Garmin's "local" timestamps are epoch values already shifted to local time, so format them as UTC."""
    if not local_epoch_ms:
        return None
    return datetime.fromtimestamp(local_epoch_ms / 1000, timezone.utc).strftime("%H:%M")


def _primary_device_entry(device_map):
    """Garmin keys these by watch ID; use the primary training device's entry."""
    entries = list((device_map or {}).values())
    return next((e for e in entries if e.get("primaryTrainingDevice")), entries[0] if entries else {})


def _training_status(ts):
    """VO2 max, training status, acute vs chronic load and monthly load balance as of the end of that day."""
    vo2 = ((ts.get("mostRecentVO2Max") or {}).get("generic")) or {}
    status = _primary_device_entry((ts.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData"))
    load = status.get("acuteTrainingLoadDTO") or {}
    balance = _primary_device_entry((ts.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap"))

    def monthly(key):
        if balance.get(f"monthlyLoad{key}") is None:
            return None
        return {
            "load": safe_round(balance[f"monthlyLoad{key}"], 0),
            "target_min": balance.get(f"monthlyLoad{key}TargetMin"),
            "target_max": balance.get(f"monthlyLoad{key}TargetMax"),
        }

    return {
        "vo2_max": vo2.get("vo2MaxPreciseValue") or vo2.get("vo2MaxValue"),
        "vo2_max_date": vo2.get("calendarDate"),
        "training_status": _phrase(status.get("trainingStatusFeedbackPhrase")),
        "training_status_date": status.get("calendarDate"),
        "training_load": {
            "acute": load.get("dailyTrainingLoadAcute"),
            "chronic": load.get("dailyTrainingLoadChronic"),
            "chronic_optimal_range": [safe_round(load["minTrainingLoadChronic"], 1), safe_round(load["maxTrainingLoadChronic"], 1)]
            if load.get("minTrainingLoadChronic") is not None else None,
            "acute_chronic_ratio": load.get("dailyAcuteChronicWorkloadRatio"),
            "ratio_status": _phrase(load.get("acwrStatus")),
        } if load else None,
        "load_balance": {
            "aerobic_low": monthly("AerobicLow"),
            "aerobic_high": monthly("AerobicHigh"),
            "anaerobic": monthly("Anaerobic"),
            "feedback": _phrase(balance.get("trainingBalanceFeedbackPhrase")),
        } if balance else None,
    }


def _sleep(sleep_data):
    """The night that ended on this date, plus any naps that day."""
    dto = sleep_data.get("dailySleepDTO") or {}
    sleep_secs = dto.get("sleepTimeSeconds")
    nap_secs = dto.get("napTimeSeconds")
    if not sleep_secs and not nap_secs:
        return None
    scores = dto.get("sleepScores") or {}

    def stage(name, secs_key):
        score = scores.get(f"{name}Percentage") or {}
        return {
            "mins": _mins(dto.get(secs_key)),
            "pct": score.get("value"),
            "optimal_pct": [score.get("optimalStart"), score.get("optimalEnd")] if score.get("optimalStart") is not None else None,
        }

    return {
        "start": _clock(dto.get("sleepStartTimestampLocal")),
        "end": _clock(dto.get("sleepEndTimestampLocal")),
        "hours": safe_round(sleep_secs / 3600) if sleep_secs else None,
        # No nap only means 0 if Garmin has sleep data for the day; otherwise it's unknown
        "nap_mins": _mins(nap_secs) if nap_secs else (0 if sleep_secs else None),
        "score": (scores.get("overall") or {}).get("value"),
        "quality": _phrase((scores.get("overall") or {}).get("qualifierKey")),
        "feedback": _phrase(dto.get("sleepScoreFeedback")),
        "stages": {
            "deep": stage("deep", "deepSleepSeconds"),
            "light": stage("light", "lightSleepSeconds"),
            "rem": stage("rem", "remSleepSeconds"),
            "awake_mins": _mins(dto.get("awakeSleepSeconds")),
        },
        "awake_count": dto.get("awakeCount"),
        "restless_moments": sleep_data.get("restlessMomentsCount"),
        "avg_hr": dto.get("avgHeartRate"),
        "avg_stress": dto.get("avgSleepStress"),
        "avg_respiration": dto.get("averageRespirationValue"),
        "avg_spo2": dto.get("averageSpO2Value"),
        "lowest_spo2": dto.get("lowestSpO2Value"),
        "breathing_disruptions": _phrase(dto.get("breathingDisruptionSeverity")),
        "body_battery_gained": sleep_data.get("bodyBatteryChange"),
    }


def _readiness(r):
    if isinstance(r, list):
        r = r[0] if r else {}
    if not r or r.get("score") is None:
        return None

    def factor(prefix):
        rating = _phrase(r.get(f"{prefix}FactorFeedback"))
        # Garmin reports factors it didn't use as 0% with feedback "NONE"
        return {"pct": r.get(f"{prefix}FactorPercent"), "rating": rating} if rating else None

    return {
        "score": r.get("score"),
        "level": _phrase(r.get("level")),
        "feedback": _phrase(r.get("feedbackLong")),
        "recovery_time_hours": safe_round(r["recoveryTime"] / 60, 1) if r.get("recoveryTime") is not None else None,
        "factors": {
            "sleep": factor("sleepScore"),
            "recovery_time": factor("recoveryTime"),
            "acute_load": factor("acwr"),
            "hrv": factor("hrv"),
            "stress_history": factor("stressHistory"),
            "sleep_history": factor("sleepHistory"),
        },
    }


def build_daily_wellness(client, date_str):
    """Returns None for days with no data at all (e.g. before the watch was set up)."""
    stats = _try("daily stats", date_str, lambda: client.get_stats(date_str), {}) or {}
    sleep_data = _try("sleep", date_str, lambda: client.get_sleep_data(date_str), {}) or {}
    hrv = (_try("HRV", date_str, lambda: client.get_hrv_data(date_str), {}) or {}).get("hrvSummary") or {}
    readiness = _try("training readiness", date_str, lambda: client.get_morning_training_readiness(date_str), {})
    training = _training_status(_try("training status", date_str, lambda: client.get_training_status(date_str), {}) or {})

    sleep = _sleep(sleep_data)
    if not stats.get("includesWellnessData") and not sleep and not hrv.get("lastNightAvg"):
        return None

    baseline = hrv.get("baseline") or {}
    distance_m = stats.get("totalDistanceMeters")
    return {
        "date": date_str,
        "workouts": [],  # filled in from the activity file when writing
        "sleep": sleep,
        "hrv": {
            "last_night_avg_ms": hrv.get("lastNightAvg"),
            "last_night_5min_high_ms": hrv.get("lastNight5MinHigh"),
            "weekly_avg_ms": hrv.get("weeklyAvg"),
            "balanced_range_ms": [baseline.get("balancedLow"), baseline.get("balancedUpper")] if baseline.get("balancedLow") else None,
            "status": _phrase(hrv.get("status")),
        } if hrv else None,
        "heart_rate": {
            "resting": stats.get("restingHeartRate"),
            "resting_7day_avg": stats.get("lastSevenDaysAvgRestingHeartRate"),
            "min": stats.get("minHeartRate"),
            "max": stats.get("maxHeartRate"),
        },
        "body_battery": {
            "at_wake": stats.get("bodyBatteryAtWakeTime"),
            "high": stats.get("bodyBatteryHighestValue"),
            "low": stats.get("bodyBatteryLowestValue"),
            "charged": stats.get("bodyBatteryChargedValue"),
            "drained": stats.get("bodyBatteryDrainedValue"),
            "end_of_day": stats.get("bodyBatteryMostRecentValue"),
        },
        "stress": {
            "avg": stats.get("averageStressLevel"),
            "max": stats.get("maxStressLevel"),
            "qualifier": _phrase(stats.get("stressQualifier")),
            "rest_mins": _mins(stats.get("restStressDuration")),
            "low_mins": _mins(stats.get("lowStressDuration")),
            "medium_mins": _mins(stats.get("mediumStressDuration")),
            "high_mins": _mins(stats.get("highStressDuration")),
        },
        "movement": {
            "steps": stats.get("totalSteps"),
            "step_goal": stats.get("dailyStepGoal"),
            "distance_mi": safe_round(distance_m / METERS_PER_MILE) if distance_m else None,
            "floors_climbed": int(round(stats["floorsAscended"])) if stats.get("floorsAscended") is not None else None,
            "intensity_minutes": {"moderate": stats.get("moderateIntensityMinutes"), "vigorous": stats.get("vigorousIntensityMinutes")},
            "active_mins": _mins((stats.get("activeSeconds") or 0) + (stats.get("highlyActiveSeconds") or 0)) if stats.get("activeSeconds") is not None else None,
            "sedentary_hours": safe_round(stats["sedentarySeconds"] / 3600, 1) if stats.get("sedentarySeconds") is not None else None,
            "active_calories": stats.get("activeKilocalories"),
            "total_calories": stats.get("totalKilocalories"),
        },
        "respiration_awake_avg": stats.get("avgWakingRespirationValue"),
        "spo2_avg": stats.get("averageSpo2"),
        "training_readiness": _readiness(readiness),
        **training,
    }


def sync_wellness(client, start, end):
    """Rebuild wellness lines for every date from start to end (inclusive) and merge them into the year files."""
    by_year = {}
    total_days = (end - start).days + 1
    day = start
    for n in range(1, total_days + 1):
        date_str = day.isoformat()
        record = build_daily_wellness(client, date_str)
        if record:
            by_year.setdefault(day.year, {})[date_str] = record
        if n % 30 == 0 or n == total_days:
            logging.info(f"Wellness progress: {n}/{total_days} days fetched (through {date_str})")
        day += timedelta(days=1)
        time.sleep(0.5)

    for year, new_records in by_year.items():
        path = wellness_path(year)
        merged = {r["date"]: r for r in read_jsonl(path)}
        merged.update(new_records)

        # Link each day to that day's workouts so rest days are obvious
        workouts = {}
        for act in read_jsonl(activities_path(year)):
            workouts.setdefault(act["date"], []).append({"activity_id": act["activity_id"], "name": act["name"], "type": act["type"]})
        for date_str, rec in merged.items():
            rec["workouts"] = workouts.get(date_str, [])

        write_jsonl(path, [merged[d] for d in sorted(merged)])
        logging.info(f"Wrote {len(new_records)} day(s) of wellness data to {path} ({len(merged)} days total)")
