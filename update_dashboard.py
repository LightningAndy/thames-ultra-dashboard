#!/usr/bin/env python3
"""
Fetches live Garmin Connect data and writes garmin_data.json.
index.html loads this file at runtime to populate all live stats.
"""
import json
import os
import sys
from datetime import date, datetime, timedelta

from garminconnect import Garmin


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def login():
    email = os.environ["GARMIN_EMAIL"]
    password = os.environ["GARMIN_PASSWORD"]
    client = Garmin(email, password)
    client.login()
    return client


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

def dig(obj, *keys, default=None):
    """Return the first matching key from obj, searching nested dicts one level deep."""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj:
                return obj[k]
        # one level of nesting
        for v in obj.values():
            if isinstance(v, dict):
                for k in keys:
                    if k in v:
                        return v[k]
    return default


def secs_to_hms(seconds):
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def day_label(iso_date_str):
    """'2026-06-07' -> '7 Jun'"""
    try:
        d = datetime.strptime(iso_date_str[:10], "%Y-%m-%d")
        return f"{d.day} {d.strftime('%b')}"
    except (ValueError, TypeError):
        return iso_date_str[:10]


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def fetch_training_status(client, today):
    raw = client.get_training_status(today)
    print("training_status keys:", list(raw.keys()) if isinstance(raw, dict) else type(raw))

    vo2 = dig(raw,
              "vo2MaxPreciseValue", "vo2_max_precise",
              "genericVO2MaxValue", "vo2Max", "vo2_max",
              default=51.0)
    vo2 = float(vo2)

    load_ratio = dig(raw,
                     "loadRatio", "load_ratio",
                     default=None)
    if load_ratio is None:
        # try nested running/cycling load ratio objects
        for nest_key in ("latestRunningLoadRatio", "latestCyclingLoadRatio"):
            nested = raw.get(nest_key) if isinstance(raw, dict) else None
            if isinstance(nested, dict):
                load_ratio = nested.get("loadRatio") or nested.get("load_ratio")
                if load_ratio is not None:
                    break
    load_ratio = float(load_ratio) if load_ratio is not None else 1.1

    tsf = dig(raw, "trainingStatusFeedback", "training_status_feedback", default="")
    tsf = str(tsf).upper()
    if "PRODUCTIVE" in tsf:
        status_str = "Productive"
    elif "MAINTAINING" in tsf:
        status_str = "Maintaining"
    elif "RECOVERY" in tsf:
        status_str = "Recovery"
    elif "OVERREACHING" in tsf:
        status_str = "Overreaching"
    elif "UNPRODUCTIVE" in tsf:
        status_str = "Unproductive"
    else:
        status_str = "Productive"

    tbf = dig(raw, "trainingBalanceFeedback", "training_balance_feedback", default="")
    tbf = str(tbf).upper()
    if "AEROBIC_LOW_SHORTAGE" in tbf:
        aerobic_val = "Low shortage"
        aerobic_sub = "more easy aerobic work needed"
    elif "AEROBIC_HIGH_SHORTAGE" in tbf:
        aerobic_val = "High shortage"
        aerobic_sub = "needs more hard efforts"
    elif "BALANCED" in tbf or not tbf:
        aerobic_val = "Balanced"
        aerobic_sub = "good aerobic mix"
    else:
        aerobic_val = "Low shortage"
        aerobic_sub = "more easy aerobic work needed"

    return {
        "vo2": vo2,
        "load_ratio": load_ratio,
        "status_str": status_str,
        "aerobic_val": aerobic_val,
        "aerobic_sub": aerobic_sub,
    }


def fetch_hrv(client, today):
    raw = client.get_hrv_data(today)
    print("hrv keys:", list(raw.keys()) if isinstance(raw, dict) else type(raw))

    summary = raw.get("hrvSummary", raw) if isinstance(raw, dict) else {}
    weekly = dig(summary, "weeklyAvg", "weekly_avg", "weeklyAvgHrvMs", default=53)
    last_night = dig(summary, "lastNight", "last_night", "lastNightAvgHrvMs", "lastNightAvg", default=61)
    status = dig(summary, "status", default="BALANCED")
    status_str = str(status).replace("_", " ").title()

    return {
        "weekly_avg": int(weekly),
        "last_night": int(last_night),
        "status": status_str,
    }


def fetch_readiness(client, today):
    raw = client.get_training_readiness(today)
    print("readiness type:", type(raw), "len:", len(raw) if isinstance(raw, list) else "N/A")

    am_score = 73
    recovery_hours = 0
    sleep_score = 85

    if isinstance(raw, list) and raw:
        # prefer morning wakeup reading for the "headline" score
        morning = next(
            (r for r in raw if "WAKEUP" in str(r.get("context", "")).upper()),
            None
        )
        # use the most recent entry for recovery hours (post-exercise)
        latest = raw[0]

        src = morning or latest
        am_score = dig(src, "score", default=am_score)
        sleep_score = dig(src, "sleepScore", "sleep_score", default=sleep_score)
        recovery_hours = dig(latest, "recoveryTimeHours", "recovery_time_hours",
                             "recoveryTime", default=recovery_hours)

    return {
        "am_score": int(am_score),
        "recovery_hours": float(recovery_hours),
        "sleep_score": int(sleep_score),
    }


def fetch_activities(client, today_dt, limit=40):
    raw = client.get_activities(0, limit)
    print(f"activities fetched: {len(raw)}")

    week_start = today_dt - timedelta(days=today_dt.weekday())

    runs = []
    run_dist_week = 0.0
    runs_this_week = 0
    today_avg_hr = None
    longest_m = 0
    longest_date_label = ""
    run_count = 0
    act_counts = {}

    for act in raw:
        atype = act.get("activityType", {})
        type_key = atype.get("typeKey", "") if isinstance(atype, dict) else str(atype)

        # count every activity type
        act_counts[type_key] = act_counts.get(type_key, 0) + 1

        if type_key != "running":
            continue

        dist_m = float(act.get("distance") or 0)
        dist_km = round(dist_m / 1000, 1)
        avg_hr = int(act.get("averageHR") or 0)
        start_str = act.get("startTimeLocal", "")

        try:
            start_dt = datetime.strptime(start_str[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            start_dt = None

        date_lbl = day_label(start_str)

        if dist_m > longest_m:
            longest_m = dist_m
            longest_date_label = date_lbl

        if start_dt and start_dt >= week_start:
            runs_this_week += 1
            run_dist_week += dist_km
            if start_dt == today_dt and today_avg_hr is None and avg_hr:
                today_avg_hr = avg_hr

        run_count += 1

        if len(runs) < 7:
            runs.append({"date": date_lbl, "dist": dist_km, "hr": avg_hr})

    # mark PB (longest distance in the list)
    longest_km = round(longest_m / 1000, 1)
    for r in runs:
        if r["dist"] == longest_km:
            r["pb"] = True
            break

    if today_avg_hr is None and runs:
        today_avg_hr = runs[0]["hr"]

    all_hrs = [r["hr"] for r in runs if r["hr"] > 0]
    avg_run_hr = int(sum(all_hrs) / len(all_hrs)) if all_hrs else 154

    return {
        "runs": runs,
        "run_count": run_count,
        "runs_this_week": runs_this_week,
        "run_dist_week": round(run_dist_week, 1),
        "longest_km": longest_km,
        "longest_date": longest_date_label,
        "today_avg_hr": today_avg_hr,
        "avg_run_hr": avg_run_hr,
        "act_counts": act_counts,
    }


def fetch_weekly_steps(client, today):
    raw = client.get_weekly_steps(today, 8)
    print("weekly_steps type:", type(raw), "len:", len(raw) if isinstance(raw, list) else "N/A")

    steps_data = []
    steps_labels = []
    dist_totals = []

    if isinstance(raw, list):
        sorted_weeks = sorted(raw, key=lambda w: dig(w, "startDate", "start_date", default=""))
        for week in sorted_weeks:
            total = dig(week, "totalSteps", "total_steps", default=0)
            dist = dig(week, "totalDistanceMeters", "total_distance_meters", default=0)
            start = dig(week, "startDate", "start_date", default="")
            steps_data.append(int(total))
            steps_labels.append(day_label(start))
            if dist:
                dist_totals.append(float(dist))

    steps_avg_k = int(sum(steps_data) / len(steps_data) / 1000) if steps_data else 110
    steps_km = int(sum(dist_totals) / len(dist_totals) / 1000) if dist_totals else 93

    return {
        "data": steps_data,
        "labels": steps_labels,
        "avg_k": steps_avg_k,
        "km_weekly": steps_km,
    }


def fetch_race_predictions(client):
    defaults = {"5k": "23:24", "10k": "50:56", "hm": "1:56:01", "marathon": "4:18:06"}
    try:
        raw = client.get_race_predictions()
        print("race_predictions type:", type(raw))
        if not isinstance(raw, list):
            return defaults
        for pred in raw:
            dist = float(dig(pred, "distanceInMeters", "distance", default=0) or 0)
            secs = dig(pred, "time", "predictionTime", "timePrediction", "seconds", default=None)
            if secs is None:
                continue
            t = secs_to_hms(float(secs))
            if abs(dist - 5000) < 200:
                defaults["5k"] = t
            elif abs(dist - 10000) < 300:
                defaults["10k"] = t
            elif abs(dist - 21097) < 500:
                defaults["hm"] = t
            elif abs(dist - 42195) < 1000:
                defaults["marathon"] = t
    except Exception as exc:
        print(f"race_predictions failed: {exc}", file=sys.stderr)
    return defaults


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    today_dt = date.today()
    today = today_dt.isoformat()
    today_label = f"{today_dt.day} {today_dt.strftime('%b %Y')}"

    print(f"Fetching Garmin data for {today} …")
    client = login()
    print("Logged in.")

    ts = fetch_training_status(client, today)
    hrv = fetch_hrv(client, today)
    rd = fetch_readiness(client, today)
    acts = fetch_activities(client, today_dt)
    steps = fetch_weekly_steps(client, today)
    preds = fetch_race_predictions(client)

    # Load ratio display and sub-text
    lr = ts["load_ratio"]
    lr_display = f"{lr:.1f}"
    if lr < 0.85:
        lr_sub = "very low · increase volume"
    elif lr < 1.0:
        lr_sub = "moderate · room to build"
    elif lr <= 1.15:
        lr_sub = "optimal · building nicely"
    elif lr <= 1.30:
        lr_sub = "elevated · monitor recovery"
    else:
        lr_sub = "high · prioritise recovery"

    # Sleep quality label
    ss = rd["sleep_score"]
    sleep_label = "Good" if ss >= 80 else "Fair" if ss >= 60 else "Poor"
    sleep_colour = "sv-green" if ss >= 80 else "sv-blue" if ss >= 60 else "sv-pink"

    # Switzerland depart: 27 Jun 2026
    switz = date(2026, 6, 27)
    days_switz = (switz - today_dt).days

    # VO2 trend — keep the early data points as confirmed history,
    # set the final point to today's live value
    vo2_int = int(ts["vo2"])
    vo2_trend_labels = ["1 Mar", "15 Mar", "1 Apr", "14 Apr", "12 May", "23 May",
                        f"{today_dt.day} {today_dt.strftime('%b')}"]
    vo2_trend_data = [50, 49, 48, 49, 50, 51, vo2_int]

    data = {
        "date": today_label,
        # fitness vitals cards
        "vo2max": vo2_int,
        "vo2maxPrecise": round(ts["vo2"], 1),
        "loadRatio": lr_display,
        "loadSub": lr_sub,
        "hrvStatus": hrv["status"],
        "hrvSub": f"{hrv['weekly_avg']} ms weekly avg · last night {hrv['last_night']} ms",
        "longestRunKm": acts["longest_km"],
        "longestRunDate": acts["longest_date"],
        "trainingStatus": ts["status_str"],
        "aerobicBalance": ts["aerobic_val"],
        "aerobicBalanceSub": ts["aerobic_sub"],
        "stepsAvgK": f"{steps['avg_k']}k",
        "stepsKmWeekly": f"~{steps['km_weekly']} km/week on foot",
        "amReadiness": rd["am_score"],
        "readinessSub": (
            f"{int(rd['recovery_hours'])} hrs recovery needed post-run"
            if rd["recovery_hours"] > 1
            else "ready to train"
        ),
        # key stats
        "runsThisWeek": f"{acts['runs_this_week']} session{'s' if acts['runs_this_week'] != 1 else ''}",
        "runDistWeek": f"{acts['run_dist_week']} km",
        "longestRunEver": f"{acts['longest_km']} km · {acts['longest_date']}",
        "todayAvgHR": (f"{acts['today_avg_hr']} bpm ✓" if acts["today_avg_hr"] else "—"),
        "totalRuns": f"{acts['run_count']} sessions",
        "predMarathon": preds["marathon"],
        "sleepScore": f"{ss}/100 · {sleep_label}",
        "sleepColour": sleep_colour,
        "switzDays": f"{days_switz} days" if days_switz > 0 else "departed",
        # fun stats
        "avgRunHR": acts["avg_run_hr"],
        # race predictions
        "pred5k": preds["5k"],
        "pred10k": preds["10k"],
        "predHM": preds["hm"],
        "predMarathonFull": preds["marathon"],
        # run list for distance log
        "runs": acts["runs"],
        # charts
        "vo2Trend": {
            "labels": vo2_trend_labels,
            "data": vo2_trend_data,
        },
        "stepsChart": {
            "data": steps["data"] or [107757, 108343, 103099, 114372, 104692, 96953, 111657, 130102],
            "labels": steps["labels"] or ["13 Apr", "20 Apr", "27 Apr", "4 May", "11 May", "18 May", "25 May", "1 Jun"],
        },
    }

    out_path = os.path.join(os.path.dirname(__file__), "garmin_data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"✓ garmin_data.json written")
    print(f"  VO2 {data['vo2max']} | Load {data['loadRatio']} | HRV {hrv['weekly_avg']} ms "
          f"| Readiness {data['amReadiness']} | Steps {data['stepsAvgK']}/wk")


if __name__ == "__main__":
    main()
