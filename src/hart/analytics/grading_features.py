"""Deterministic inputs for session grading (spec–8.3.2).

Everything Claude grades from is computed here from the database and frozen
into ``session_grades.features``, so every grade can be traced back to the
exact facts it was based on.
"""

from __future__ import annotations

import datetime
import json
import math
from statistics import mean, median, pstdev
from typing import Any

from hart.server.data import (
    DAY,
    INDOOR_SUBTYPES,
    is_indoor,
    one,
    phase_on,
    readiness_on,
    rows,
)
from hart.storage.database import Database

GRADED_SPORTS = ("swim", "bike", "run", "strength")
MIN_DURATION_S = 10 * 60
COMPARE_WINDOW_DAYS = 90
COMPARE_FALLBACK_DAYS = 365
MIN_COMPARABLE = 3
STREAM_MIN_S = 20 * 60


def _r(value: float | None, digits: int = 1) -> float | None:
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return None
    return round(value, digits)


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def eligibility(activity: dict[str, Any], in_event: bool) -> tuple[bool, str | None]:
    """(gradable, reason if not).  Events are gradable (context fit only)."""
    sport = activity.get("sport_type")
    duration = activity.get("moving_seconds") or activity.get("elapsed_seconds") or 0
    if sport not in GRADED_SPORTS:
        sub = activity.get("sub_type") or "other"
        return False, f"Not a training session ({sport}/{sub}: walks, hikes, yoga etc. aren't graded)"
    if duration < MIN_DURATION_S:
        return False, "Shorter than 10 minutes"
    if sport != "strength" and not (
        activity.get("avg_hr") or activity.get("avg_power") or activity.get("avg_pace_sec_km")
    ):
        return False, "No heart rate, power or pace recorded"
    return True, None


# ---------------------------------------------------------------------------
# Streams: durability
# ---------------------------------------------------------------------------


def stream_durability(db: Database, activity_id: str, sport: str) -> dict[str, Any] | None:
    """First-half vs second-half efficiency and HR drift from the streams.

    Bike uses power ÷ HR, run uses speed ÷ HR; only moving samples (power or
    speed > 0) with a heart rate count.  Decoupling > 5% on a steady session
    means endurance didn't hold for that duration.
    """
    output = "power" if sport == "bike" else "speed"
    if sport not in ("bike", "run"):
        return None
    samples = db.fetchall(
        f"SELECT heart_rate, {output} FROM activity_streams WHERE activity_id = ? "
        f"AND heart_rate > 40 AND {output} > 0 ORDER BY timestamp_sec",
        [activity_id],
    )
    if len(samples) < STREAM_MIN_S:
        return None
    half = len(samples) // 2
    first, second = samples[:half], samples[half:]
    hr1, hr2 = mean(s[0] for s in first), mean(s[0] for s in second)
    out1, out2 = mean(s[1] for s in first), mean(s[1] for s in second)
    ef1, ef2 = out1 / hr1, out2 / hr2
    return {
        "basis": "power/HR" if sport == "bike" else "speed/HR",
        "decoupling_pct": _r((ef1 - ef2) / ef1 * 100, 1),
        "hr_first_half": _r(hr1, 0),
        "hr_second_half": _r(hr2, 0),
        "output_first_half": _r(out1, 1),
        "output_second_half": _r(out2, 1),
        "output_unit": "W" if sport == "bike" else "km/h",
        "samples": len(samples),
    }


def lap_variability(laps: list[dict[str, Any]], sport: str) -> dict[str, Any] | None:
    key = "avg_power" if sport == "bike" else "avg_pace_sec_km"
    values = [lap[key] for lap in laps if lap.get(key) and (lap.get("elapsed_seconds") or 0) >= 60]
    if len(values) < 3:
        return None
    return {"laps": len(values), "metric": key, "cv_pct": _r(pstdev(values) / mean(values) * 100, 1)}


# ---------------------------------------------------------------------------
# Comparison with similar sessions
# ---------------------------------------------------------------------------


def comparable_sessions(db: Database, activity: dict[str, Any], injury: dict[str, Any] | None) -> dict[str, Any]:
    sport = activity["sport_type"]
    duration = activity.get("moving_seconds") or activity.get("elapsed_seconds") or 0
    indoor = is_indoor(activity.get("sub_type"))
    placeholders = ", ".join("?" for _ in INDOOR_SUBTYPES)
    indoor_clause = f"a.sub_type IN ({placeholders})" if indoor else f"coalesce(a.sub_type, '') NOT IN ({placeholders})"

    def fetch(days: int) -> list[dict[str, Any]]:
        return rows(
            db,
            "SELECT a.activity_id, CAST(a.start_time AS DATE) AS date, a.avg_hr, a.avg_power, a.normalized_power, "
            "a.avg_pace_sec_km, coalesce(a.moving_seconds, a.elapsed_seconds) AS secs, m.tss AS load, "
            "m.efficiency_factor AS ef FROM activities a LEFT JOIN activity_metrics m USING (activity_id) "
            f"WHERE a.sport_type = ? AND {indoor_clause} AND a.start_time < ? AND a.start_time >= ? "
            "AND coalesce(a.moving_seconds, a.elapsed_seconds) BETWEEN ? AND ? ORDER BY a.start_time DESC",
            [
                sport,
                *INDOOR_SUBTYPES,
                activity["start_time"],
                activity["start_time"] - datetime.timedelta(days=days),
                duration * 0.75,
                duration * 1.25,
            ],
        )

    window = COMPARE_WINDOW_DAYS
    similar = fetch(window)
    if len(similar) < MIN_COMPARABLE:
        window = COMPARE_FALLBACK_DAYS
        similar = fetch(window)
    pre_injury = bool(injury and similar and any(s["date"] <= injury["start_date"] for s in similar))

    def med(key: str) -> float | None:
        vals = [s[key] for s in similar if s.get(key) not in (None, 0)]
        return median(vals) if vals else None

    this = {
        "avg_hr": activity.get("avg_hr"),
        "avg_power": activity.get("avg_power"),
        "normalized_power": activity.get("normalized_power"),
        "avg_pace_sec_km": activity.get("avg_pace_sec_km"),
        "load": activity.get("load"),
        "ef": activity.get("efficiency_factor"),
    }
    medians = {k: _r(med(k), 3 if k == "ef" else 1) for k in this}
    deltas = {
        k: _r(((this[k] - medians[k]) / medians[k]) * 100, 1)
        for k in this
        if this[k] not in (None, 0) and medians.get(k)
    }
    return {
        "count": len(similar),
        "window_days": window,
        "indoor": indoor,
        "pre_injury_baseline": pre_injury,
        "criteria": f"same sport, {'indoor' if indoor else 'outdoor'}, duration ±25%, last {window} days",
        "notes": "this_vs_median_pct: + means higher than the median. avg_pace_sec_km is seconds per km, "
        "so + means SLOWER. For avg_hr at similar output, lower is better.",
        "medians": {k: v for k, v in medians.items() if v is not None},
        "this_vs_median_pct": deltas,
        "recent": [{"date": s["date"], "avg_hr": _r(s["avg_hr"], 0), "load": _r(s["load"], 0)} for s in similar[:5]],
    }


# ---------------------------------------------------------------------------
# Strength
# ---------------------------------------------------------------------------


def strength_summary(db: Database, activity: dict[str, Any]) -> dict[str, Any] | None:
    """Top set per lift, with the previous session's top weight for the same lift.

    Lifts are grouped with :func:`hart.analytics.strength_progress.exercise_key`, so a set Garmin
    recorded as "DEADLIFT" and one recorded as "BARBELL_DEADLIFT" count as the same lift."""
    from hart.analytics.strength_progress import exercise_key, exercise_label, load_aliases

    if activity["sport_type"] != "strength":
        return None
    aliases = load_aliases(db)
    sets = rows(
        db,
        "SELECT exercise_name, exercise_category, repetitions, weight_kg "
        "FROM strength_sets WHERE activity_id = ? AND set_type = 'active' ORDER BY set_index",
        [activity["activity_id"]],
    )
    by_ex: dict[str, list[dict[str, Any]]] = {}
    for s in sets:
        key = exercise_key(s["exercise_name"], s["exercise_category"], aliases) or "UNKNOWN"
        by_ex.setdefault(key, []).append(s)
    # Previous top weight per lift: walk earlier sessions, newest first, until every lift is found.
    previous: dict[str, dict[str, Any]] = {}
    earlier = rows(
        db,
        "SELECT CAST(a.start_time AS DATE) AS date, s.exercise_name, s.exercise_category, s.weight_kg "
        "FROM strength_sets s JOIN activities a USING (activity_id) WHERE s.set_type = 'active' "
        "AND a.start_time < ? AND a.activity_id <> ? ORDER BY a.start_time DESC",
        [activity["start_time"], activity["activity_id"]],
    )
    for e in earlier:
        key = exercise_key(e["exercise_name"], e["exercise_category"], aliases)
        if key not in by_ex:
            continue
        found = previous.get(key)
        if found is None:
            previous[key] = {"date": e["date"], "top_kg": e["weight_kg"]}
        elif found["date"] == e["date"] and (e["weight_kg"] or 0) > (found["top_kg"] or 0):
            found["top_kg"] = e["weight_kg"]
        if len(previous) == len(by_ex) and all(p["date"] != e["date"] for p in previous.values()):
            break
    out = []
    for key, ex_sets in by_ex.items():
        top = max(ex_sets, key=lambda s: (s["weight_kg"] or 0, s["repetitions"] or 0))
        out.append(
            {
                "exercise": exercise_label(key),
                "key": key,
                "sets": len(ex_sets),
                "top_kg": top["weight_kg"],
                "top_reps": top["repetitions"],
                "previous": previous.get(key),
            }
        )
    return {"exercises": out, "total_sets": len(sets)} if sets else {"exercises": [], "total_sets": 0}


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------


def _single_sided(db: Database) -> bool:
    from hart.server import settings  # the athlete's equipment (Settings → Athlete)

    return bool(settings.get(db, "power_single_sided"))


def build_features(db: Database, activity_id: str, thresholds: dict[str, float]) -> dict[str, Any] | None:
    a = one(
        db,
        "SELECT a.*, m.tss AS load, m.tss_method, m.hr_zone_seconds, m.efficiency_factor, m.aerobic_decoupling_pct, "
        "f.rpe AS my_rpe, f.feel AS my_feel, f.comment AS my_comment "
        "FROM activities a LEFT JOIN activity_metrics m USING (activity_id) "
        "LEFT JOIN session_feedback f USING (activity_id) WHERE a.activity_id = ?",
        [activity_id],
    )
    if a is None:
        return None
    day = a["start_time"].date()
    annotations = rows(
        db,
        "SELECT kind, label, start_date, end_date FROM annotations WHERE start_date <= ? "
        "AND (end_date IS NULL OR end_date >= ?)",
        [day, day],
    )
    event = next((x for x in annotations if x["kind"] == "event"), None)
    injury = one(
        db,
        "SELECT start_date, end_date, label FROM annotations WHERE kind = 'injury' "
        "AND start_date <= ? ORDER BY start_date DESC LIMIT 1",
        [day],
    )
    gradable, reason = eligibility(a, bool(event))

    zones = a.get("hr_zone_seconds")
    if isinstance(zones, str):
        zones = json.loads(zones)
    zone_pct = None
    if zones:
        total = sum(v for v in zones.values() if v) or 0
        zone_pct = {k: _r(v / total * 100, 0) for k, v in sorted(zones.items())} if total else None

    laps = rows(
        db,
        "SELECT lap_index, elapsed_seconds, distance_meters, avg_hr, avg_power, avg_pace_sec_km "
        "FROM activity_laps WHERE activity_id = ? ORDER BY lap_index",
        [activity_id],
    )
    streams = db.fetchone(
        "SELECT count(*), count(heart_rate), count(power) FROM activity_streams WHERE activity_id = ?", [activity_id]
    )
    load_day = one(
        db, "SELECT ctl, atl, tsb FROM daily_training_load WHERE sport_type = 'combined' AND date = ?", [day - DAY]
    )
    readiness = readiness_on(db, day, thresholds)
    notes = rows(
        db,
        "SELECT category, title, rules FROM athlete_notes WHERE status = 'active' "
        "AND category IN ('injury', 'constraint') AND (valid_to IS NULL OR valid_to >= ?)",
        [day],
    )
    phase = phase_on(db, day)
    rpe = a.get("my_rpe") if a.get("my_rpe") is not None else (round(a["rpe"] / 10) if a.get("rpe") else None)
    feel = (
        a.get("my_feel")
        if a.get("my_feel") is not None
        else (round(a["feel"] / 25) + 1 if a.get("feel") is not None else None)
    )
    duration = a.get("moving_seconds") or a.get("elapsed_seconds")

    return {
        "activity_id": activity_id,
        "gradable": gradable,
        "ungraded_reason": reason,
        "event": event["label"] if event else None,
        "basics": {
            "sport": a["sport_type"],
            "sub_type": a.get("sub_type"),
            "indoor": is_indoor(a.get("sub_type")),
            "name": a.get("name"),
            "date": day,
            "start": a["start_time"],
            "duration_min": _r(duration / 60, 0) if duration else None,
            "distance_km": _r((a.get("distance_meters") or 0) / 1000, 2) or None,
            "elevation_m": _r(a.get("total_elevation_m"), 0),
            "load": _r(a.get("load"), 0),
            "load_method": a.get("tss_method"),
            "avg_hr": _r(a.get("avg_hr"), 0),
            "max_hr": _r(a.get("max_hr"), 0),
            "avg_power_w": _r(a.get("avg_power"), 0),
            "normalized_power_w": _r(a.get("normalized_power"), 0),
            "avg_pace_min_km": _r(a["avg_pace_sec_km"] / 60, 2) if a.get("avg_pace_sec_km") else None,
            "avg_cadence": _r(a.get("avg_cadence"), 0),
            "training_effect_aerobic": _r(a.get("training_effect_aerobic"), 1),
            "training_effect_anaerobic": _r(a.get("training_effect_anaerobic"), 1),
            "training_effect_label": a.get("training_effect_label"),
        },
        "intensity": {"hr_zone_pct": zone_pct},
        "durability": {
            "notes": "Decoupling compares the first and second half of the whole session; it is only meaningful "
            "for steady sessions — warm-ups, intervals, stops or hills inflate it. Check lap_variability "
            "(cv_pct above ~8% = not steady).",
            "efficiency_factor": _r(a.get("efficiency_factor"), 3),
            "stream_decoupling": stream_durability(db, activity_id, a["sport_type"]),
            "lap_variability": lap_variability(laps, a["sport_type"]),
        },
        "comparison": comparable_sessions(db, a, injury) if a["sport_type"] in ("bike", "run", "swim") else None,
        "strength": strength_summary(db, a),
        "plan": None,  # the plan row this session matched (not linked into grading yet)
        "feedback": {"rpe": rpe, "feel": feel, "comment": a.get("my_comment")},
        "context": {
            "phase": {
                "name": phase["name"],
                "type": phase["phase_type"],
                "week": phase["week"],
                "goal": phase.get("goal"),
            }
            if phase
            else None,
            "readiness_that_morning": {"level": readiness["level"], "reason": readiness["reason"]},
            "form_day_before": {k: _r(v, 1) for k, v in (load_day or {}).items()},
            "active_constraints": [{"title": n["title"], "rules": n["rules"]} for n in notes],
            "annotations": [{"kind": x["kind"], "label": x["label"]} for x in annotations],
            "injury": {"label": injury["label"], "start": injury["start_date"], "end": injury["end_date"]}
            if injury
            else None,
        },
        "data_quality": {
            "stream_samples": streams[0],
            "hr_samples": streams[1],
            "power_samples": streams[2],
            "laps": len(laps),
            "single_sided_power": a["sport_type"] == "bike" and bool(a.get("avg_power")) and _single_sided(db),
        },
    }
