"""Builds one JSONL workout record from a Garmin activity. Shared by garminsync.py and garminsync_backlog.py."""
import time
import logging
from datetime import datetime, timezone

METERS_PER_MILE = 1609.34
FEET_PER_METER = 3.28084

# Run/walk detection segments shorter than this are treated as noise and merged into the previous segment
MIN_SEGMENT_SECS = 20

# Garmin's self-evaluation "How did you feel?" values
FEEL_LABELS = {0: "Very Weak", 25: "Weak", 50: "Normal", 75: "Strong", 100: "Very Strong"}

def safe_round(val, decimals=2):
    try:
        return round(float(val), decimals)
    except (TypeError, ValueError):
        return val


def is_run(activity_type):
    return "run" in activity_type


def is_ride(activity_type):
    return "cycle" in activity_type or "bike" in activity_type


def pace_per_mile(speed_ms):
    if not speed_ms or speed_ms <= 0:
        return None
    sec_per_mile = METERS_PER_MILE / speed_ms
    return f"{int(sec_per_mile // 60)}:{int(sec_per_mile % 60):02d}/mi"


def cm_to_m(val):
    return safe_round(val / 100) if isinstance(val, (int, float)) else None


def _try(label, act_id, fn, default=None):
    try:
        return fn()
    except Exception as e:
        logging.warning(f"   ↳ Could not retrieve {label} for ID {act_id}: {e}")
        return default


def _hr_zones(client, act_id):
    """Time in each HR zone, with Garmin's bpm boundaries so the zone numbers are unambiguous."""
    zones = _try("HR zones", act_id, lambda: client.get_activity_hr_in_timezones(act_id), []) or []
    zones = sorted(zones, key=lambda z: z.get("zoneNumber", 0))
    result = []
    for i, z in enumerate(zones):
        next_low = zones[i + 1].get("zoneLowBoundary") if i + 1 < len(zones) else None
        result.append({
            "zone": z.get("zoneNumber"),
            "min_bpm": z.get("zoneLowBoundary"),
            "max_bpm": next_low - 1 if next_low else None,
            "mins": safe_round(z.get("secsInZone", 0) / 60),
        })
    return result


def mmss(secs):
    secs = int(round(secs))
    return f"{secs // 60}:{secs % 60:02d}"


def _gmt_secs(ts):
    return datetime.fromisoformat(ts[:19]).replace(tzinfo=timezone.utc).timestamp()


def _merge_segments(splits):
    """Turn Garmin's run/walk detection splits into clean run and walk segments.

    Garmin's detection is noisy (2-second "walks" mid-run, 10-second "runs" during a cooldown),
    so any segment shorter than MIN_SEGMENT_SECS is folded into the segment before it.
    """
    raw = sorted(
        (s for s in splits if s.get("type") in ("RWD_RUN", "RWD_WALK", "RWD_STAND")),
        key=lambda s: s["startTimeGMT"],
    )
    segments = []
    for s in raw:
        dur = s.get("duration") or 0
        seg = {
            "kind": "run" if s["type"] == "RWD_RUN" else "walk",
            "start": _gmt_secs(s["startTimeGMT"]),
            "dur": dur,
            "dist": s.get("distance") or 0,
            "hr_x_dur": (s.get("averageHR") or 0) * dur,
            "cad_x_dur": (s.get("averageRunCadence") or 0) * dur,
            "max_hr": s.get("maxHR") or 0,
        }
        if segments and (dur < MIN_SEGMENT_SECS or segments[-1]["kind"] == seg["kind"]):
            prev = segments[-1]
            for k in ("dur", "dist", "hr_x_dur", "cad_x_dur"):
                prev[k] += seg[k]
            prev["max_hr"] = max(prev["max_hr"], seg["max_hr"])
        else:
            segments.append(seg)

    # A blip at the very start has nothing before it, so fold it into the next segment instead
    if len(segments) > 1 and segments[0]["dur"] < MIN_SEGMENT_SECS:
        first, second = segments.pop(0), segments[0]
        for k in ("dur", "dist", "hr_x_dur", "cad_x_dur"):
            second[k] += first[k]
        second["start"] = first["start"]
        second["max_hr"] = max(first["max_hr"], second["max_hr"])
    return segments


def _stream(client, act_id):
    """Second-by-second HR and Performance Condition, keyed by GMT epoch seconds."""
    details = _try("HR stream", act_id, lambda: client.get_activity_details(act_id, maxchart=10000, maxpoly=0))
    if not details:
        return None
    idx = {m["key"]: m["metricsIndex"] for m in details.get("metricDescriptors", [])}
    if "directTimestamp" not in idx or "directHeartRate" not in idx:
        return None
    points = []
    for p in details.get("activityDetailMetrics", []):
        m = p["metrics"]
        ts = m[idx["directTimestamp"]]
        if ts is None:
            continue
        pc = m[idx["directPerformanceCondition"]] if "directPerformanceCondition" in idx else None
        points.append((ts / 1000, m[idx["directHeartRate"]], pc))
    return points


def _run_walk(client, act_id):
    """Break a run/walk session into its run intervals, recovery walks and any warm-up/cool-down walk.

    Walk breaks between runs are part of the run (e.g. run up to ~160 bpm, walk down to ~135, repeat),
    so they're reported as recovery walks, separate from a warm-up or cool-down walk.
    """
    typed = _try("run/walk splits", act_id, lambda: client.get_activity_typed_splits(act_id), {}) or {}
    segments = _merge_segments(typed.get("splits", []))
    run_idx = [i for i, s in enumerate(segments) if s["kind"] == "run"]
    if not run_idx:
        return None

    points = _stream(client, act_id) or []
    t0 = segments[0]["start"]

    def window(seg):
        return [p for p in points if seg["start"] <= p[0] < seg["start"] + seg["dur"]]

    def hr_at(pts, pick):
        vals = [p[1] for p in pts if p[1]]
        return pick(vals) if vals else None

    def totals(segs):
        dur = sum(s["dur"] for s in segs)
        dist = sum(s["dist"] for s in segs)
        return {
            "mins": safe_round(dur / 60),
            "distance_mi": safe_round(dist / METERS_PER_MILE),
            "pace": pace_per_mile(dist / dur if dur else 0),
            "avg_hr": safe_round(sum(s["hr_x_dur"] for s in segs) / dur) if dur else None,
            "cadence_spm": safe_round(sum(s["cad_x_dur"] for s in segs) / dur) if dur else None,
        }

    first_run, last_run = run_idx[0], run_idx[-1]
    out_segments = []
    run_no = 0
    for i, seg in enumerate(segments):
        pts = window(seg)
        base = {
            "start": mmss(seg["start"] - t0),
            "duration": mmss(seg["dur"]),
            "distance_mi": safe_round(seg["dist"] / METERS_PER_MILE),
            "avg_hr": safe_round(seg["hr_x_dur"] / seg["dur"]) if seg["dur"] else None,
        }
        if seg["kind"] == "run":
            run_no += 1
            pcs = [p[2] for p in pts if p[2] is not None]
            out_segments.append({
                "type": "run", "n": run_no, **base,
                "pace": pace_per_mile(seg["dist"] / seg["dur"] if seg["dur"] else 0),
                "cadence_spm": safe_round(seg["cad_x_dur"] / seg["dur"]) if seg["dur"] else None,
                "start_hr": hr_at(pts[:5], lambda v: v[0]),
                "peak_hr": hr_at(pts, max) or seg["max_hr"],
                "perf_condition_end": pcs[-1] if pcs else None,
            })
        else:
            kind = "warmup_walk" if i < first_run else "cooldown_walk" if i > last_run else "recovery_walk"
            walk = {"type": kind, **base, "start_hr": hr_at(pts[:5], lambda v: v[0])}
            if kind == "recovery_walk":
                walk["low_hr"] = hr_at(pts, min)
                walk["end_hr"] = hr_at(pts[-5:], lambda v: v[-1])
            else:
                walk["pace"] = pace_per_mile(seg["dist"] / seg["dur"] if seg["dur"] else 0)
            out_segments.append(walk)

    runs = [segments[i] for i in run_idx]
    recoveries = [s for s in out_segments if s["type"] == "recovery_walk"]
    recovery_segs = [segments[i] for i in range(first_run, last_run) if segments[i]["kind"] == "walk"]
    cooldown = [segments[i] for i in range(last_run + 1, len(segments))]
    block_end = segments[last_run]["start"] + segments[last_run]["dur"]
    pcs = [p[2] for p in points if p[2] is not None]

    result = {
        "run_intervals": len(runs),
        "interval_block": {
            "start": mmss(segments[first_run]["start"] - t0),
            "end": mmss(block_end - t0),
            **totals(segments[first_run:last_run + 1]),
        },
        "running_only": totals(runs),
        "recovery_walks": {
            "count": len(recovery_segs),
            "total_mins": safe_round(sum(s["dur"] for s in recovery_segs) / 60),
            "avg_secs": safe_round(sum(s["dur"] for s in recovery_segs) / len(recovery_segs)) if recovery_segs else None,
            "avg_low_hr": safe_round(sum(r["low_hr"] for r in recoveries if r["low_hr"]) / len(recoveries)) if recoveries and all(r["low_hr"] for r in recoveries) else None,
        },
        "cooldown_walk": totals(cooldown) if cooldown else None,
        "performance_condition": {"first": pcs[0], "peak": max(pcs), "end_of_intervals": next((p[2] for p in reversed(points) if p[0] <= block_end and p[2] is not None), None), "final": pcs[-1]} if pcs else None,
        "segments": out_segments,
    }
    return result


def build_workout_record(client, act):
    act_id = act.get("activityId")
    activity_name = act.get("activityName", "Workout")
    activity_type = act.get("activityType", {}).get("typeKey", "unknown")
    start_date = act.get("startTimeLocal", "Unknown Date").split(" ")[0]

    time.sleep(1.0)
    detail = _try("activity detail", act_id, lambda: client.get_activity(act_id), {}) or {}
    summary = detail.get("summaryDTO") or {}

    benefit = act.get("trainingEffectLabel")
    feel = summary.get("directWorkoutFeel")
    rpe = summary.get("directWorkoutRpe")

    workout_record = {
        "date": start_date,
        "activity_id": act_id,
        "name": activity_name,
        "type": activity_type,
        "duration_mins": safe_round(act.get("duration", 0) / 60),
        "calories": safe_round(act.get("calories", 0)),
        "heart_rate": {
            "avg": safe_round(act.get("averageHR", None)),
            "max": safe_round(act.get("maxHR", None)),
        },
        "hr_zones": _hr_zones(client, act_id),
        "training_effect": {
            "aerobic": safe_round(act.get("aerobicTrainingEffect", None)),
            "anaerobic": safe_round(act.get("anaerobicTrainingEffect", None)),
            "exercise_load": safe_round(act.get("activityTrainingLoad", None)),
            "primary_benefit": benefit.replace("_", " ").title() if benefit else None,
        },
        "intensity_minutes": {
            "moderate": act.get("moderateIntensityMinutes"),
            "vigorous": act.get("vigorousIntensityMinutes"),
        },
        "self_evaluation": {
            "feel": FEEL_LABELS.get(feel, feel),
            "perceived_effort": rpe / 10 if isinstance(rpe, (int, float)) else None,
        },
        "body_battery_impact": act.get("differenceBodyBattery", summary.get("differenceBodyBattery")),
        "stamina": {
            "start_pct": summary.get("beginPotentialStamina"),
            "end_pct": summary.get("endPotentialStamina"),
            "min_pct": summary.get("minAvailableStamina"),
        },
        "metrics": {},
        "laps": [],
    }

    distance_m = act.get("distance") or 0
    moving_secs = act.get("movingDuration")
    elevation = {
        "total_ascent_ft": safe_round((act.get("elevationGain") or 0) * FEET_PER_METER),
        "total_descent_ft": safe_round((act.get("elevationLoss") or 0) * FEET_PER_METER),
    }

    # Sport-specific metrics
    if is_run(activity_type):
        workout_record["metrics"] = {
            "distance_miles": safe_round(distance_m / METERS_PER_MILE),
            "avg_pace": pace_per_mile(act.get("averageSpeed")),
            "moving_mins": safe_round(moving_secs / 60) if moving_secs else None,
            "avg_moving_pace": pace_per_mile(distance_m / moving_secs) if moving_secs else None,
            **elevation,
            "cadence_spm": safe_round(act.get("averageRunningCadenceInStepsPerMinute", None)),
            "stride_length_m": cm_to_m(act.get("avgStrideLength")),
            "ground_contact_time_ms": safe_round(act.get("avgGroundContactTime", None)),
            "vertical_oscillation_cm": safe_round(act.get("avgVerticalOscillation", None)),
            "vertical_ratio_pct": safe_round(act.get("avgVerticalRatio", None)),
        }
        run_walk = _run_walk(client, act_id)
        if run_walk:
            workout_record["run_walk"] = run_walk
    elif is_ride(activity_type):
        workout_record["metrics"] = {
            "distance_miles": safe_round(distance_m / METERS_PER_MILE),
            "avg_speed_mph": safe_round((act.get("averageSpeed") or 0) * 2.23694),
            "moving_mins": safe_round(moving_secs / 60) if moving_secs else None,
            **elevation,
            "cadence_rpm": safe_round(act.get("averageBikingCadenceInRpm", None)),
        }

    avg_power = act.get("averagePower")
    if avg_power:
        workout_record["metrics"]["power_watts_avg"] = safe_round(avg_power)

    # Laps
    raw_splits = _try("lap data", act_id, lambda: client.get_activity_splits(act_id))
    lap_splits = []
    if isinstance(raw_splits, dict):
        lap_splits = raw_splits.get("lapDTOs") or raw_splits.get("activitySplits") or raw_splits.get("lapSplits") or []
    elif isinstance(raw_splits, list):
        lap_splits = raw_splits
    if not lap_splits:
        summaries = _try("split summaries", act_id, lambda: client.get_activity_split_summaries(act_id))
        if isinstance(summaries, list):
            lap_splits = summaries

    for lap_idx, split in enumerate(lap_splits, start=1):
        speed_ms = split.get("speed", split.get("averageSpeed")) or 0
        lap = {
            "lap": split.get("lapIndex", split.get("splitIndex", lap_idx)),
            "distance_mi": safe_round(split.get("distance", 0) / METERS_PER_MILE),
            "duration_secs": safe_round(split.get("duration", split.get("elapsedDuration", 0))),
            "pace_or_speed": None,
            "avg_hr": safe_round(split.get("averageHR", split.get("averageHeartRate", None))),
            "max_hr": safe_round(split.get("maxHR", split.get("maxHeartRate", None))),
            "elevation_gain_ft": safe_round((split.get("elevationGain") or 0) * FEET_PER_METER),
            "elevation_loss_ft": safe_round((split.get("elevationLoss") or 0) * FEET_PER_METER),
        }
        if split.get("averagePower"):
            lap["power_watts_avg"] = safe_round(split["averagePower"])
        if is_run(activity_type):
            lap["pace_or_speed"] = pace_per_mile(speed_ms)
            lap["cadence_spm"] = safe_round(split.get("averageRunCadence", None))
            lap["stride_length_m"] = cm_to_m(split.get("strideLength"))
            lap["ground_contact_time_ms"] = safe_round(split.get("groundContactTime", None))
            lap["vertical_oscillation_cm"] = safe_round(split.get("verticalOscillation", None))
            lap["vertical_ratio_pct"] = safe_round(split.get("verticalRatio", None))
        elif is_ride(activity_type) and speed_ms > 0:
            lap["pace_or_speed"] = f"{safe_round(speed_ms * 2.23694)} mph"
        workout_record["laps"].append(lap)

    logging.info(f"   ↳ Built record for ID {act_id} with {len(workout_record['laps'])} lap(s)")
    return workout_record
