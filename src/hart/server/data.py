"""Read models for the web UI and API.

Every function takes a cursor-backed ``Database`` and returns plain data.
Missing values are ``None`` — never 0 and never NaN (DuckDB returns NaN for
some stored metrics, which is invalid JSON).
"""

from __future__ import annotations

import datetime
import json
import math
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from hart.analytics.phase import STATE_LABELS, TRAINING_SPORTS, observed_state, phase_flags
from hart.analytics.readiness import ReadinessInputs, compute_readiness
from hart.config import HartSettings
from hart.server import state
from hart.storage.database import Database

D = datetime.date
DAY = datetime.timedelta(days=1)
INDOOR_SUBTYPES = ("indoor_cycling", "virtual_activity", "treadmill")
SPORTS = ("swim", "bike", "run", "strength", "other")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def clean(value: Any) -> Any:
    """Recursively replace NaN/inf with None and parse JSON strings we stored."""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if value is not None and type(value).__name__ == "NaTType":
        return None
    return value


def rows(db: Database, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    cur = db.connection.execute(sql, params or [])
    names = [c[0] for c in cur.description]
    return [clean(dict(zip(names, r))) for r in cur.fetchall()]


def one(db: Database, sql: str, params: list[Any] | None = None) -> dict[str, Any] | None:
    result = rows(db, sql, params)
    return result[0] if result else None


def local_today(config: HartSettings) -> D:
    return datetime.datetime.now(ZoneInfo(config.server.tz)).date()


def monday(d: D) -> D:
    return d - datetime.timedelta(days=d.weekday())


def is_indoor(sub_type: str | None) -> bool:
    return (sub_type or "") in INDOOR_SUBTYPES


def _hours(seconds: float | None) -> float:
    return round((seconds or 0) / 3600, 2)


# ---------------------------------------------------------------------------
# Season context: phase, observed state, flags
# ---------------------------------------------------------------------------


def phase_on(db: Database, d: D) -> dict[str, Any] | None:
    phase = one(
        db,
        "SELECT id, phase_type, name, start_date, end_date, goal, source, confirmed "
        "FROM training_phases WHERE start_date <= ? AND end_date >= ? ORDER BY start_date DESC LIMIT 1",
        [d, d],
    )
    if phase:
        phase["week"] = (d - phase["start_date"]).days // 7 + 1
        phase["weeks"] = ((phase["end_date"] - phase["start_date"]).days) // 7 + 1
    return phase


def training_days(db: Database, since: D, until: D) -> set[D]:
    placeholders = ", ".join("?" for _ in TRAINING_SPORTS)
    return {
        r[0]
        for r in db.fetchall(
            "SELECT DISTINCT CAST(start_time AS DATE) FROM activities "
            f"WHERE sport_type IN ({placeholders}) AND start_time >= ? AND start_time < ?",
            [*TRAINING_SPORTS, since, until + DAY],
        )
    }


def season_context(db: Database, d: D, t: dict[str, float]) -> dict[str, Any]:
    load = rows(
        db,
        "SELECT date, ctl, atl, tsb FROM daily_training_load "
        "WHERE sport_type = 'combined' AND date > ? AND date <= ? ORDER BY date",
        [d - datetime.timedelta(days=90), d],
    )
    history_days = db.fetchone(
        "SELECT count(*) FROM daily_training_load WHERE sport_type = 'combined' AND date <= ?", [d]
    )[0]
    days = training_days(db, d - datetime.timedelta(days=30), d)
    t_hist = {**t, "state_min_history_days": 0} if history_days >= t["state_min_history_days"] else t
    current = observed_state(load, days, d, t_hist)
    recent = [observed_state(load, days, d - i * DAY, t_hist)["state"] for i in range(14)]
    phase = phase_on(db, d)
    return {
        "phase": phase,
        "state": {**current, "label": STATE_LABELS[current["state"]]},
        "flags": phase_flags(phase["phase_type"] if phase else None, current, recent, t),
    }


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def readiness_on(db: Database, d: D, t: dict[str, float]) -> dict[str, Any]:
    n = int(t["ready_baseline_days"])
    sleep = one(db, "SELECT total_sleep_sec, sleep_score FROM sleep_records WHERE date = ?", [d]) or {}
    hrv = one(db, "SELECT hrv_last_night_ms FROM hrv_daily WHERE date = ?", [d]) or {}
    health = (
        one(db, "SELECT resting_hr, body_battery_start, training_readiness FROM daily_health WHERE date = ?", [d]) or {}
    )
    recovery = one(db, "SELECT recovery_score FROM daily_recovery WHERE date = ?", [d]) or {}
    tsb = one(db, "SELECT tsb FROM daily_training_load WHERE sport_type = 'combined' AND date = ?", [d - DAY]) or {}
    since = d - datetime.timedelta(days=n)
    hrv_hist = [
        r["v"]
        for r in rows(
            db,
            "SELECT hrv_last_night_ms AS v FROM hrv_daily WHERE date >= ? AND date < ? AND hrv_last_night_ms IS NOT NULL",
            [since, d],
        )
        if r["v"] is not None
    ]
    rhr_hist = [
        r["v"]
        for r in rows(
            db,
            "SELECT resting_hr AS v FROM daily_health WHERE date >= ? AND date < ? AND resting_hr IS NOT NULL",
            [since, d],
        )
        if r["v"] is not None
    ]
    critical = [
        r["description"]
        for r in rows(
            db,
            "SELECT description FROM anomaly_log WHERE severity = 'critical' AND NOT acknowledged "
            "AND CAST(detected_at AS DATE) > ?",
            [d - datetime.timedelta(days=3)],
        )
    ]

    def active(kind: str) -> str | None:
        row = one(
            db,
            "SELECT label FROM annotations WHERE kind = ? AND start_date <= ? "
            "AND (end_date IS NULL OR end_date >= ?) LIMIT 1",
            [kind, d, d],
        )
        return row["label"] if row else None

    sleep_sec = sleep.get("total_sleep_sec")
    result = compute_readiness(
        ReadinessInputs(
            sleep_hours=round(sleep_sec / 3600, 2) if sleep_sec else None,
            sleep_score=sleep.get("sleep_score"),
            hrv_last_night=hrv.get("hrv_last_night_ms"),
            hrv_history=hrv_hist,
            resting_hr=health.get("resting_hr"),
            rhr_history=rhr_hist,
            recovery_score=recovery.get("recovery_score"),
            body_battery=health.get("body_battery_start"),
            training_readiness=health.get("training_readiness"),
            tsb_yesterday=tsb.get("tsb"),
            critical_anomalies=critical,
            illness=active("illness"),
            injury=active("injury"),
        ),
        t,
    )
    return {"date": d, **result}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

SESSION_COLUMNS = (
    "a.activity_id, a.sport_type, a.sub_type, a.name, a.start_time, a.elapsed_seconds, a.moving_seconds, "
    "a.distance_meters, a.total_elevation_m, a.avg_hr, a.max_hr, a.avg_power, a.normalized_power, "
    "a.avg_cadence, a.avg_pace_sec_km, a.avg_speed_kmh, a.training_effect_aerobic, "
    "a.training_effect_anaerobic, a.training_effect_label, a.rpe AS garmin_rpe, a.feel AS garmin_feel, "
    "m.tss AS load, m.efficiency_factor, m.aerobic_decoupling_pct, m.hr_zone_seconds, "
    "f.rpe, f.feel, f.comment"
)
SESSION_FROM = (
    "FROM activities a LEFT JOIN activity_metrics m USING (activity_id) "
    "LEFT JOIN session_feedback f USING (activity_id)"
)


def _session_row(r: dict[str, Any]) -> dict[str, Any]:
    seconds = r.get("moving_seconds") or r.get("elapsed_seconds")
    r["duration_s"] = seconds
    r["indoor"] = is_indoor(r.get("sub_type"))
    r["date"] = r["start_time"].date() if r.get("start_time") else None
    if isinstance(r.get("hr_zone_seconds"), str):
        r["hr_zone_seconds"] = json.loads(r["hr_zone_seconds"])
    return r


def with_grades(db: Database, sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from hart.server.grading import grade_chips

    chips = grade_chips(db, [s["activity_id"] for s in sessions])
    for s in sessions:
        s["grade"] = chips.get(s["activity_id"])
    return sessions


def recent_sessions(db: Database, since: D, until: D) -> list[dict[str, Any]]:
    return with_grades(
        db,
        [
            _session_row(r)
            for r in rows(
                db,
                f"SELECT {SESSION_COLUMNS} {SESSION_FROM} WHERE a.start_time >= ? AND a.start_time < ? "
                "ORDER BY a.start_time DESC",
                [since, until + DAY],
            )
        ],
    )


def week_summary(db: Database, today: D) -> dict[str, Any]:
    start = monday(today)
    sessions = recent_sessions(db, start, start + 6 * DAY)
    days = []
    for i in range(7):
        d = start + i * DAY
        day_sessions = [s for s in sessions if s["date"] == d]
        days.append(
            {
                "date": d,
                "is_today": d == today,
                "sessions": [
                    {"sport": s["sport_type"], "minutes": round((s["duration_s"] or 0) / 60), "name": s["name"]}
                    for s in sorted(day_sessions, key=lambda s: s["start_time"])
                ],
            }
        )
    totals = {
        "hours": round(sum((s["duration_s"] or 0) for s in sessions) / 3600, 1),
        "load": round(sum(s["load"] or 0 for s in sessions)),
        "sessions": len(sessions),
    }
    prev = rows(
        db,
        "SELECT date_trunc('week', a.start_time) AS wk, sum(coalesce(a.moving_seconds, a.elapsed_seconds)) AS secs, "
        "sum(m.tss) AS load FROM activities a LEFT JOIN activity_metrics m USING (activity_id) "
        "WHERE a.start_time >= ? AND a.start_time < ? GROUP BY 1",
        [start - 28 * DAY, start],
    )
    avg = {
        "hours": round(sum(r["secs"] or 0 for r in prev) / 4 / 3600, 1),
        "load": round(sum(r["load"] or 0 for r in prev) / 4),
    }
    return {"start": start, "days": days, "totals": totals, "avg_4w": avg}


def per_sport_ctl(db: Database, today: D, ctl_tc: int = 42) -> list[dict[str, Any]]:
    """Fitness (CTL) per training sport, as of today.

    Each sport's load series ends on its last activity, so read every sport's
    own latest row (not the latest date overall, which is usually just one
    sport) and decay it to today with the CTL time constant — the same decay
    the load model applies on days without that sport.
    """
    placeholders = ", ".join("?" for _ in TRAINING_SPORTS)
    latest = rows(
        db,
        "SELECT t.sport_type, t.date, t.ctl FROM daily_training_load t "
        f"WHERE t.sport_type IN ({placeholders}) AND t.date = ("
        "  SELECT max(date) FROM daily_training_load WHERE sport_type = t.sport_type AND date <= ?)",
        [*TRAINING_SPORTS, today],
    )
    out = []
    for r in latest:
        if r["ctl"] is None:
            continue
        days = (today - r["date"]).days
        ctl = r["ctl"] * math.exp(-days / ctl_tc)
        if ctl >= 0.5:
            out.append({"sport_type": r["sport_type"], "ctl": round(ctl, 1), "last_date": r["date"]})
    order = {s: i for i, s in enumerate(TRAINING_SPORTS)}
    return sorted(out, key=lambda r: order[r["sport_type"]])


def load_card(db: Database, today: D, ctl_tc: int = 42) -> dict[str, Any]:
    series = rows(
        db,
        "SELECT date, ctl, atl, tsb, daily_tss FROM daily_training_load "
        "WHERE sport_type = 'combined' AND date > ? AND date <= ? ORDER BY date",
        [today - 90 * DAY, today],
    )
    latest = series[-1] if series else None
    week_ago = next((r for r in reversed(series) if r["date"] <= today - 7 * DAY), None)
    return {
        "latest": latest,
        "ramp_7d": round(latest["ctl"] - week_ago["ctl"], 1) if latest and week_ago else None,
        "series": series,
        "per_sport": per_sport_ctl(db, today, ctl_tc),
    }


TOKEN_LIFETIME_DAYS = 365
TOKEN_WARN_DAYS = 30


PURPOSE_LABELS = {
    "chat": "Chat",
    "grade": "Grading",
    "suggest": "Suggestions",
    "garmin_steps": "Garmin workouts",
    "garmin_text": "Garmin text",
    "parse_plan": "Plan paste",
    "parse_labs": "Lab paste",
}


PURPOSE_COLORS = {  # the app's palette, one per kind of run (usage bars and table)
    "chat": "#e27d60",
    "suggest": "#6eb886",
    "grade": "#e2a45f",
    "parse_plan": "#6fa8c9",
    "parse_labs": "#c49bd6",
}


def claude_usage(db: Database, today: D, days: int = 14) -> dict[str, Any]:
    """Claude usage per day and purpose. The measure is the API-equivalent cost each run reports: runs vary
    a lot in size, and cached tokens are cheap, so neither run counts nor raw tokens track how much of the
    plan's limit is used. Runs from before the cost was recorded count as runs only."""
    since = today - datetime.timedelta(days=days - 1)
    runs = rows(
        db,
        "SELECT CAST(started_at AS DATE) AS day, purpose, model, status, duration_ms, "
        "TRY_CAST(transcript->>'cost_usd' AS DOUBLE) AS cost "
        "FROM claude_runs WHERE started_at >= ?",
        [since],
    )
    purposes: dict[str, dict[str, Any]] = {}
    by_day: dict[D, dict[str, float]] = {since + datetime.timedelta(days=i): {} for i in range(days)}
    runs_by_day: dict[D, dict[str, int]] = {d: {} for d in by_day}
    uncosted_by_day: dict[D, int] = dict.fromkeys(by_day, 0)
    for r in runs:
        p = purposes.setdefault(
            r["purpose"],
            {
                "purpose": r["purpose"],
                "label": PURPOSE_LABELS.get(r["purpose"], r["purpose"]),
                "color": PURPOSE_COLORS.get(r["purpose"], "#8ba393"),
                "runs": 0,
                "runs_today": 0,
                "failed": 0,
                "minutes": 0.0,
                "cost": 0.0,
                "cost_today": 0.0,
                "models": set(),
            },
        )
        cost = r["cost"] or 0.0
        p["runs"] += 1
        p["runs_today"] += r["day"] == today
        p["failed"] += r["status"] not in ("ok", "running")
        p["minutes"] += (r["duration_ms"] or 0) / 60000
        p["cost"] += cost
        p["cost_today"] += cost if r["day"] == today else 0.0
        p["models"].add(r["model"])
        if r["day"] in by_day:
            by_day[r["day"]][r["purpose"]] = by_day[r["day"]].get(r["purpose"], 0.0) + cost
            runs_by_day[r["day"]][r["purpose"]] = runs_by_day[r["day"]].get(r["purpose"], 0) + 1
            uncosted_by_day[r["day"]] += r["cost"] is None
    for p in purposes.values():
        p["minutes"] = round(p["minutes"], 1)
        p["models"] = sorted(p["models"])
    ordered = sorted(purposes.values(), key=lambda p: (-p["cost"], -p["runs"]))
    has_cost = any(r["cost"] is not None for r in runs)
    # Bars show cost once runs record it; before that (and for older days) they show run counts.
    per_day = []
    for d, costs in by_day.items():
        counts = runs_by_day[d]
        value = costs if has_cost else counts
        per_day.append(
            {
                "day": d,
                "cost": sum(costs.values()),
                "runs": sum(counts.values()),
                "uncosted": uncosted_by_day[d],
                "value": sum(value.values()),
                "parts": [
                    {
                        "label": p["label"],
                        "color": p["color"],
                        "cost": costs.get(p["purpose"], 0.0),
                        "runs": counts.get(p["purpose"], 0),
                        "value": value.get(p["purpose"], 0),
                    }
                    for p in ordered
                    if value.get(p["purpose"])
                ],
            }
        )
    limit = one(
        db,
        "SELECT finished_at, transcript->>'resets_at' AS resets_at FROM claude_runs WHERE status = 'usage_limited' "
        "ORDER BY id DESC LIMIT 1",
    )
    return {
        "days": days,
        "purposes": ordered,
        "per_day": per_day,
        "top_day": max((d["value"] for d in per_day), default=0),
        "cost_total": sum(p["cost"] for p in ordered),
        "cost_today": sum(p["cost_today"] for p in ordered),
        "has_cost": has_cost,
        "total_runs": len(runs),
        "today_runs": sum(1 for r in runs if r["day"] == today),
        "last_limit": limit,
    }


def claude_status(db: Database, config: HartSettings, today: D) -> dict[str, Any]:
    """Claude token expiry (from CLAUDE_TOKEN_ISSUED_AT) and the latest run problem."""
    expires = None
    days_left = None
    if config.claude.token_issued_at:
        try:
            expires = D.fromisoformat(config.claude.token_issued_at) + datetime.timedelta(days=TOKEN_LIFETIME_DAYS)
            days_left = (expires - today).days
        except ValueError:
            pass
    last = one(
        db,
        "SELECT status, error, finished_at, transcript FROM claude_runs WHERE status <> 'running' "
        "ORDER BY id DESC LIMIT 1",
    )
    problem = None
    if last and last["status"] in ("auth_failed", "usage_limited"):
        problem = {"status": last["status"], "error": last["error"], "at": last["finished_at"]}
        if isinstance(last.get("transcript"), str):
            problem["resets_at"] = json.loads(last["transcript"]).get("resets_at")
    return {
        "token_expires": expires,
        "token_days_left": days_left,
        "token_warning": days_left is not None and days_left <= TOKEN_WARN_DAYS,
        "problem": problem,
    }


def alerts(db: Database, config: HartSettings | None = None) -> dict[str, Any]:
    now = datetime.datetime.now(tz=datetime.UTC)
    from hart.server.jobs.scheduler import garmin_blocked

    backup = one(
        db,
        "SELECT status, error, finished_at FROM jobs WHERE type = 'backup' AND status IN ('ok', 'error') "
        "ORDER BY id DESC LIMIT 1",
    )
    return {
        "anomalies": rows(
            db,
            "SELECT id, severity, anomaly_type, metric_name, description, detected_at "
            "FROM anomaly_log WHERE NOT acknowledged ORDER BY detected_at DESC LIMIT 20",
        ),
        "proposed_notes": db.fetchone("SELECT count(*) FROM athlete_notes WHERE status = 'proposed'")[0],
        "season_proposals": db.fetchone(
            "SELECT count(*) FROM season_proposals WHERE status = 'pending' AND kind NOT IN ('plan', 'plan_import')"
        )[0],
        "plan_proposals": db.fetchone(
            "SELECT count(*) FROM season_proposals WHERE status = 'pending' AND kind IN ('plan', 'plan_import')"
        )[0],
        "health_overdue": db.fetchone(
            "SELECT count(*) FROM health_checks WHERE status = 'open' AND due_date <= ?",
            [local_today(config) if config else datetime.date.today()],
        )[0],
        "health_proposed": db.fetchone("SELECT count(*) FROM health_checks WHERE status = 'proposed'")[0],
        "garmin_blocked": garmin_blocked(db, now),
        "backup_failed": backup if backup and backup["status"] == "error" else None,
        "claude": claude_status(db, config, local_today(config)) if config else None,
    }


def header_status(db: Database, config: HartSettings) -> dict[str, Any]:
    """Status pill in the masthead: is the system syncing normally?"""
    from hart.server.jobs.scheduler import garmin_blocked, last_successful_sync

    now = datetime.datetime.now(tz=datetime.UTC)
    blocked = garmin_blocked(db, now)
    last = last_successful_sync(db)
    age_h = None if last is None else (now - last).total_seconds() / 3600
    if state.is_demo(db):
        level, text, detail = "warn", "Demo data", "A fictional athlete — syncs and Claude are off (hart demo)"
    elif blocked == "garmin_auth":
        level, text, detail = "err", "Garmin login expired", "Scheduled syncs are paused until `hart auth` runs"
    elif last is None or (age_h is not None and age_h > 24):
        level, text, detail = "err", "Sync stale", "No successful sync in over 24 hours"
    elif blocked == "garmin_rate_limited" or (age_h is not None and age_h > 3):
        level, text, detail = "warn", "Sync delayed", "Last successful sync over 3 hours ago"
    else:
        level, text, detail = "", "System active", "Syncing normally"
    return {"level": level, "text": text, "detail": detail, "env": config.server.env, "tz": config.server.tz}


def build_dashboard(db: Database, config: HartSettings) -> dict[str, Any]:
    from hart.server import suggestions
    from hart.server.jobs.scheduler import last_successful_sync

    t = state.get_thresholds(db)
    today = local_today(config)
    race = one(
        db,
        "SELECT id, name, race_date, distance, priority FROM races WHERE priority = 'A' AND race_date >= ? "
        "ORDER BY race_date LIMIT 1",
        [today],
    )
    # A B/C race coming up before the A-race is worth seeing on the dashboard too.
    next_race = one(
        db,
        "SELECT id, name, race_date, distance, priority FROM races WHERE race_date >= ? AND race_date < ? "
        "AND priority <> 'A' ORDER BY race_date LIMIT 1",
        [today, race["race_date"] if race else datetime.date.max],
    )
    for r in (race, next_race):
        if r:
            r["days_to_race"] = (r["race_date"] - today).days
    last_sync = last_successful_sync(db)
    age = None
    if last_sync is not None:
        age = int((datetime.datetime.now(tz=datetime.UTC) - last_sync).total_seconds() // 60)
    return {
        "today": today,
        "race": race,
        "next_race": next_race,
        "season": season_context(db, today, t),
        "readiness": readiness_on(db, today, t),
        "load": load_card(db, today, config.analytics.ctl_time_constant),
        "last_7_days": recent_sessions(db, today - 6 * DAY, today),
        "week": week_summary(db, today),
        "alerts": alerts(db, config),
        "suggest": suggestions.dashboard_card(db, config),
        "last_successful_sync": last_sync,
        "sync_age_minutes": age,
        "demo": state.is_demo(db),
    }


# ---------------------------------------------------------------------------
# Fitness
# ---------------------------------------------------------------------------

RANGES = {"90d": 90, "180d": 180, "1y": 365, "all": 36500}


def _range_start(today: D, range_key: str) -> D:
    return today - RANGES.get(range_key, 90) * DAY


def backgrounds(db: Database, start: D, end: D) -> dict[str, Any]:
    """Phase bands and annotations overlapping [start, end] for charts."""
    return {
        "phases": rows(
            db,
            "SELECT phase_type, name, start_date, end_date FROM training_phases "
            "WHERE end_date >= ? AND start_date <= ? ORDER BY start_date",
            [start, end],
        ),
        "annotations": rows(
            db,
            "SELECT kind, label, start_date, coalesce(end_date, ?) AS end_date FROM annotations "
            "WHERE coalesce(end_date, ?) >= ? AND start_date <= ? ORDER BY start_date",
            [end, end, start, end],
        ),
    }


def pmc(db: Database, today: D, range_key: str, sport: str) -> dict[str, Any]:
    start = _range_start(today, range_key)
    series = rows(
        db,
        "SELECT date, ctl, atl, tsb, daily_tss FROM daily_training_load "
        "WHERE sport_type = ? AND date >= ? AND date <= ? ORDER BY date",
        [sport, start, today],
    )
    first = series[0]["date"] if series else start
    return {"sport": sport, "series": series, **backgrounds(db, first, today)}


def weekly_volume(db: Database, today: D, weeks: int) -> dict[str, Any]:
    start = monday(today) - (weeks - 1) * 7 * DAY
    data = rows(
        db,
        "SELECT CAST(date_trunc('week', a.start_time) AS DATE) AS week, a.sport_type AS sport, "
        "sum(coalesce(a.moving_seconds, a.elapsed_seconds)) AS secs, sum(m.tss) AS load, count(*) AS n "
        "FROM activities a LEFT JOIN activity_metrics m USING (activity_id) "
        "WHERE a.start_time >= ? GROUP BY 1, 2 ORDER BY 1",
        [start],
    )
    week_list = [start + i * 7 * DAY for i in range(weeks)]
    hours = {s: [0.0] * weeks for s in SPORTS}
    load = [0.0] * weeks
    for r in data:
        i = (r["week"] - start).days // 7
        if 0 <= i < weeks:
            hours.setdefault(r["sport"], [0.0] * weeks)[i] += _hours(r["secs"])
            load[i] += r["load"] or 0
    return {
        "weeks": week_list,
        "hours": {s: [round(v, 2) for v in vals] for s, vals in hours.items() if any(vals)},
        "load": [round(v) for v in load],
    }


def efficiency(db: Database, today: D, range_key: str) -> dict[str, Any]:
    start = _range_start(today, range_key)
    data = rows(
        db,
        "SELECT a.activity_id, a.name, CAST(a.start_time AS DATE) AS date, a.sport_type AS sport, a.sub_type, "
        "coalesce(a.moving_seconds, a.elapsed_seconds) AS secs, m.efficiency_factor AS ef, "
        "m.aerobic_decoupling_pct AS decoupling "
        "FROM activities a JOIN activity_metrics m USING (activity_id) "
        "WHERE a.sport_type IN ('bike', 'run') AND a.start_time >= ? ORDER BY a.start_time",
        [start],
    )
    out: dict[str, Any] = {}
    for sport in ("bike", "run"):
        pts = [r for r in data if r["sport"] == sport and r["ef"] is not None and r["ef"] > 0]
        for p in pts:
            p["indoor"] = is_indoor(p["sub_type"])
        rolling = []
        for p in pts:
            window = [q["ef"] for q in pts if p["date"] - 28 * DAY < q["date"] <= p["date"]]
            rolling.append({"date": p["date"], "ef": round(median(window), 3)})
        decoupling = [
            {"date": r["date"], "decoupling": r["decoupling"], "name": r["name"], "activity_id": r["activity_id"]}
            for r in data
            if r["sport"] == sport and r["decoupling"] is not None and (r["secs"] or 0) >= 45 * 60
        ]
        out[sport] = {"points": pts, "rolling": rolling, "decoupling": decoupling}
    return out


def health_series(db: Database, today: D, range_key: str) -> dict[str, Any]:
    start = _range_start(today, range_key)
    p = [start, today]
    return {
        "hrv": rows(
            db,
            "SELECT date, hrv_last_night_ms AS last_night, hrv_weekly_avg_ms AS weekly, "
            "baseline_low_ms AS low, baseline_high_ms AS high FROM hrv_daily "
            "WHERE date >= ? AND date <= ? AND (hrv_last_night_ms IS NOT NULL OR hrv_weekly_avg_ms IS NOT NULL) "
            "ORDER BY date",
            p,
        ),
        "rhr": rows(
            db,
            "SELECT date, resting_hr AS value FROM daily_health WHERE date >= ? AND date <= ? "
            "AND resting_hr IS NOT NULL ORDER BY date",
            p,
        ),
        "sleep": rows(
            db,
            "SELECT date, round(total_sleep_sec / 3600.0, 2) AS hours, sleep_score AS score "
            "FROM sleep_records WHERE date >= ? AND date <= ? AND total_sleep_sec IS NOT NULL "
            "ORDER BY date",
            p,
        ),
        "recovery": rows(
            db,
            "SELECT date, recovery_score AS value FROM daily_recovery WHERE date >= ? AND date <= ? ORDER BY date",
            p,
        ),
        "vo2max": rows(
            db,
            "SELECT date, vo2max_run AS run, vo2max_cycle AS cycle FROM daily_health "
            "WHERE date >= ? AND date <= ? AND (vo2max_run IS NOT NULL OR vo2max_cycle IS NOT NULL) "
            "ORDER BY date",
            p,
        ),
        **backgrounds(db, start, today),
    }


# Power curves per activity never change once recorded: cache them.
_PDC_CACHE: dict[str, dict[int, float]] = {}


def _activity_pdc(db: Database, activity_id: str) -> dict[int, float]:
    if activity_id not in _PDC_CACHE:
        from hart.analytics.power import power_duration_curve

        series = [
            r[0] or 0
            for r in db.fetchall(
                "SELECT power FROM activity_streams WHERE activity_id = ? ORDER BY timestamp_sec",
                [activity_id],
            )
        ]
        _PDC_CACHE[activity_id] = power_duration_curve(series) if sum(1 for v in series if v) >= 60 else {}
    return _PDC_CACHE[activity_id]


def best_power(db: Database, start: D, end: D) -> dict[int, float]:
    best: dict[int, float] = {}
    ids = [
        r[0]
        for r in db.fetchall(
            "SELECT activity_id FROM activities WHERE sport_type = 'bike' AND start_time >= ? AND start_time < ? "
            "AND avg_power > 0",
            [start, end + DAY],
        )
    ]
    for activity_id in ids:
        for dur, watts in _activity_pdc(db, activity_id).items():
            if watts > best.get(dur, 0):
                best[dur] = watts
    return best


def power_curves(db: Database, today: D) -> dict[str, Any]:
    last = best_power(db, today - 89 * DAY, today)
    prev = best_power(db, today - 179 * DAY, today - 90 * DAY)
    all_time = best_power(db, D(2000, 1, 1), today)
    durations = sorted(set(last) | set(prev) | set(all_time))
    return {
        "durations": durations,
        "last_90d": [last.get(d) for d in durations],
        "previous_90d": [prev.get(d) for d in durations],
        "all_time": [all_time.get(d) for d in durations],
    }


def markers(db: Database, today: D) -> list[dict[str, Any]]:
    """Current markers with change vs ~90 days earlier; missing ones omitted."""
    out: list[dict[str, Any]] = []
    then = today - 90 * DAY

    def latest(column: str, before: D) -> dict[str, Any] | None:
        return one(
            db,
            f"SELECT date, {column} AS value FROM daily_health WHERE {column} IS NOT NULL AND date <= ? "
            "ORDER BY date DESC LIMIT 1",
            [before],
        )

    for key, label, unit, column in (
        ("vo2max_run", "VO2max run", "", "vo2max_run"),
        ("vo2max_cycle", "VO2max bike", "", "vo2max_cycle"),
        ("resting_hr", "Resting HR", "bpm", "resting_hr"),
    ):
        now = latest(column, today)
        if now:
            before = latest(column, then)
            out.append(
                {
                    "key": key,
                    "label": label,
                    "unit": unit,
                    "value": now["value"],
                    "date": now["date"],
                    "delta": round(now["value"] - before["value"], 1) if before else None,
                }
            )

    from hart.server import settings

    single_sided = settings.get(db, "power_single_sided")
    last = best_power(db, today - 89 * DAY, today)
    prev = best_power(db, today - 179 * DAY, today - 90 * DAY)
    for dur, label in ((300, "Best 5 min power"), (1200, "Best 20 min power"), (3600, "Best 60 min power")):
        if dur in last:
            out.append(
                {
                    "key": f"power_{dur}",
                    "label": label,
                    "unit": "W",
                    "value": last[dur],
                    "date": None,
                    "delta": round(last[dur] - prev[dur], 1) if dur in prev else None,
                    "note": "last 90 days vs previous 90" + (" · single-sided power" if single_sided else ""),
                }
            )
    return out


# ---------------------------------------------------------------------------
# Season page
# ---------------------------------------------------------------------------


def season(db: Database, today: D) -> dict[str, Any]:
    phases = rows(
        db,
        "SELECT id, phase_type, name, start_date, end_date, goal, source, confirmed FROM training_phases "
        "ORDER BY start_date",
    )
    races = rows(db, "SELECT id, name, race_date, distance, priority, notes FROM races ORDER BY race_date")
    annotations = rows(db, "SELECT id, kind, label, start_date, end_date, source FROM annotations ORDER BY start_date")
    starts = [p["start_date"] for p in phases] + [a["start_date"] for a in annotations]
    ends = [p["end_date"] for p in phases] + [r["race_date"] for r in races]
    start = monday(min(starts) if starts else today - 180 * DAY)
    end = max(ends) if ends else today + 180 * DAY
    weekly = rows(
        db,
        "SELECT CAST(date_trunc('week', date) AS DATE) AS week, round(sum(daily_tss)) AS load "
        "FROM daily_training_load WHERE sport_type = 'combined' AND date >= ? AND date <= ? GROUP BY 1 ORDER BY 1",
        [start, today],
    )
    detection = state.get_setting(db, "season_detection")
    proposals = rows(
        db,
        "SELECT id, kind, action, summary, reason, created_at FROM season_proposals "
        "WHERE status = 'pending' ORDER BY id",
    )
    return {
        "proposals": proposals,
        "today": today,
        "start": start,
        "end": end,
        "phases": phases,
        "races": races,
        "annotations": annotations,
        "weekly_load": weekly,
        "detection": detection,
        "unconfirmed": sum(1 for p in phases if not p["confirmed"]),
    }


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def sessions_list(
    db: Database,
    sport: str | None,
    start: D | None,
    end: D | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    clauses, params = [], []
    if sport:
        clauses.append("a.sport_type = ?")
        params.append(sport)
    if start:
        clauses.append("a.start_time >= ?")
        params.append(start)
    if end:
        clauses.append("a.start_time < ?")
        params.append(end + DAY)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    total = db.fetchone(f"SELECT count(*) FROM activities a {where}", params)[0]
    items = with_grades(
        db,
        [
            _session_row(r)
            for r in rows(
                db,
                f"SELECT {SESSION_COLUMNS} {SESSION_FROM} {where} ORDER BY a.start_time DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        ],
    )
    return {"total": total, "items": items, "limit": limit, "offset": offset}


def session_detail(db: Database, activity_id: str) -> dict[str, Any] | None:
    row = one(db, f"SELECT {SESSION_COLUMNS}, a.description {SESSION_FROM} WHERE a.activity_id = ?", [activity_id])
    if row is None:
        return None
    detail = _session_row(row)
    detail["laps"] = rows(
        db,
        "SELECT lap_index, elapsed_seconds, moving_seconds, distance_meters, avg_hr, max_hr, avg_power, "
        "avg_cadence, avg_pace_sec_km, total_elevation_m FROM activity_laps WHERE activity_id = ? ORDER BY lap_index",
        [activity_id],
    )
    detail["strength_sets"] = rows(
        db,
        "SELECT set_index, set_type, exercise_name, exercise_category, repetitions, weight_kg, duration_sec "
        "FROM strength_sets WHERE activity_id = ? AND set_type = 'active' ORDER BY set_index",
        [activity_id],
    )
    from hart.server.grading import grade_chips, latest_grade

    detail["grade"] = latest_grade(db, activity_id)
    detail["grade_pending"] = bool(grade_chips(db, [activity_id]).get(activity_id, {}).get("pending"))
    detail["grade_versions"] = [
        r[0]
        for r in db.fetchall(
            "SELECT version FROM session_grades WHERE activity_id = ? ORDER BY version DESC", [activity_id]
        )
    ]
    if detail["grade"] and detail["grade"].get("claude_run_id"):
        run = one(
            db,
            "SELECT transcript, model, num_turns, duration_ms FROM claude_runs WHERE id = ?",
            [detail["grade"]["claude_run_id"]],
        )
        if run:
            transcript = (
                json.loads(run["transcript"]) if isinstance(run["transcript"], str) else (run["transcript"] or {})
            )
            detail["grade_run"] = {
                "model": run["model"],
                "turns": run["num_turns"],
                "ms": run["duration_ms"],
                "tool_calls": transcript.get("tool_calls", []),
            }
    detail["has_streams"] = db.fetchone(
        "SELECT count(*) > 0 FROM activity_streams WHERE activity_id = ?", [activity_id]
    )[0]
    detail["feedback"] = {
        "rpe": detail.get("rpe")
        if detail.get("rpe") is not None
        else (round(detail["garmin_rpe"] / 10) if detail.get("garmin_rpe") else None),
        "feel": detail.get("feel")
        if detail.get("feel") is not None
        else (round(detail["garmin_feel"] / 25) + 1 if detail.get("garmin_feel") is not None else None),
        "comment": detail.get("comment"),
        "saved": detail.get("rpe") is not None or detail.get("feel") is not None or bool(detail.get("comment")),
    }
    return detail


def session_streams(db: Database, activity_id: str, max_points: int = 1000) -> dict[str, Any]:
    bounds = db.fetchone(
        "SELECT min(timestamp_sec), max(timestamp_sec) FROM activity_streams WHERE activity_id = ?", [activity_id]
    )
    if not bounds or bounds[0] is None:
        return {"t": [], "series": {}}
    t0, t1 = bounds
    bucket = max(1, math.ceil((t1 - t0 + 1) / max_points))
    data = rows(
        db,
        "SELECT (timestamp_sec - ?) // ? AS b, min(timestamp_sec - ?) AS t, avg(heart_rate) AS hr, "
        "avg(power) AS power, avg(cadence) AS cadence, avg(speed) AS speed, avg(altitude) AS altitude "
        "FROM activity_streams WHERE activity_id = ? GROUP BY 1 ORDER BY 1",
        [t0, bucket, t0, activity_id],
    )
    series = {}
    for key in ("hr", "power", "cadence", "speed", "altitude"):
        values = [None if r[key] is None else round(r[key], 1) for r in data]
        if any(v not in (None, 0) for v in values):
            series[key] = values
    return {"t": [r["t"] for r in data], "series": series, "bucket_s": bucket}


# ---------------------------------------------------------------------------
# Session side-by-side (this session vs a similar earlier one)
# ---------------------------------------------------------------------------

COMPARE_CANDIDATES = 12
COMPARE_DAYS = 365


def compare_candidates(db: Database, s: dict[str, Any]) -> list[dict[str, Any]]:
    """Earlier sessions like this one: same sport and indoor/outdoor, duration ±25%
    (any duration for strength), last year, most recent first."""
    placeholders = ", ".join("?" for _ in INDOOR_SUBTYPES)
    indoor = f"a.sub_type IN ({placeholders})" if s["indoor"] else f"coalesce(a.sub_type, '') NOT IN ({placeholders})"
    params: list[Any] = [
        s["sport_type"],
        *INDOOR_SUBTYPES,
        s["start_time"],
        s["start_time"] - datetime.timedelta(days=COMPARE_DAYS),
    ]
    duration = ""
    if s["sport_type"] != "strength" and s.get("duration_s"):
        duration = "AND coalesce(a.moving_seconds, a.elapsed_seconds) BETWEEN ? AND ? "
        params += [s["duration_s"] * 0.75, s["duration_s"] * 1.25]
    return rows(
        db,
        "SELECT a.activity_id, a.name, a.start_time, coalesce(a.moving_seconds, a.elapsed_seconds) AS secs "
        f"FROM activities a WHERE a.sport_type = ? AND {indoor} AND a.start_time < ? AND a.start_time >= ? "
        f"{duration}ORDER BY a.start_time DESC LIMIT {COMPARE_CANDIDATES}",
        params,
    )


def _zone_pct(zones: Any) -> dict[str, int]:
    if not isinstance(zones, dict):
        return {}
    total = sum(v for v in zones.values() if v)
    return {z: round(100 * v / total) for z, v in sorted(zones.items()) if v} if total else {}


def session_compare(db: Database, activity_id: str, other_id: str | None = None) -> dict[str, Any] | None:
    from hart.analytics.grading_features import strength_summary

    def load(aid: str) -> dict[str, Any] | None:
        row = one(db, f"SELECT {SESSION_COLUMNS} {SESSION_FROM} WHERE a.activity_id = ?", [aid])
        return _session_row(row) if row else None

    this = load(activity_id)
    if this is None:
        return None
    candidates = compare_candidates(db, this)
    ids = [c["activity_id"] for c in candidates]
    other_id = other_id if other_id in ids else (ids[0] if ids else None)
    if other_id is None:
        return {"candidates": [], "other": None, "rows": []}
    other = load(other_id)

    def rpe(x: dict[str, Any]) -> float | None:
        return (
            x.get("rpe") if x.get("rpe") is not None else (round(x["garmin_rpe"] / 10) if x.get("garmin_rpe") else None)
        )

    # (label, key or getter, unit, digits, better): better = "up" / "down" when the direction is clear.
    metrics: list[tuple[str, Any, str, int, str | None]] = [
        ("Duration", lambda x: round((x["duration_s"] or 0) / 60), "min", 0, None),
        (
            "Distance",
            lambda x: round(x["distance_meters"] / 1000, 2) if x.get("distance_meters") else None,
            "km",
            2,
            None,
        ),
        ("Avg HR", "avg_hr", "bpm", 0, None),
        ("Max HR", "max_hr", "bpm", 0, None),
    ]
    if this["sport_type"] == "bike":
        metrics += [
            ("Avg power", "avg_power", "W", 0, None),
            ("Normalized power", "normalized_power", "W", 0, None),
            ("Cadence", "avg_cadence", "rpm", 0, None),
        ]
    elif this["sport_type"] == "run":
        metrics += [("Pace", "avg_pace_sec_km", "pace", 0, "down"), ("Cadence", "avg_cadence", "spm", 0, None)]
    elif this["sport_type"] == "swim":
        metrics += [("Speed", "avg_speed_kmh", "km/h", 2, "up")]
    if this["sport_type"] != "strength":
        metrics += [
            ("Efficiency factor", "efficiency_factor", "", 2, "up"),
            ("Decoupling", "aerobic_decoupling_pct", "%", 1, "down"),
        ]
    metrics += [
        ("Load", "load", "", 0, None),
        ("Aerobic TE", "training_effect_aerobic", "", 1, None),
        ("RPE", rpe, "/10", 0, None),
    ]

    out_rows = []
    for label, key, unit, digits, better in metrics:
        get = key if callable(key) else (lambda x, k=key: x.get(k))
        a, b = get(this), get(other)
        if a is None and b is None:
            continue
        delta = round((a - b) / b * 100, 1) if isinstance(a, int | float) and isinstance(b, int | float) and b else None
        verdict = None
        if delta is not None and better and abs(delta) >= 1:
            verdict = "good" if (delta > 0) == (better == "up") else "bad"
        out_rows.append(
            {
                "label": label,
                "this": a,
                "other": b,
                "unit": unit,
                "digits": digits,
                "delta_pct": delta,
                "verdict": verdict,
            }
        )

    zones = {"this": _zone_pct(this.get("hr_zone_seconds")), "other": _zone_pct(other.get("hr_zone_seconds"))}
    exercises = []
    if this["sport_type"] == "strength":
        mine = {e["exercise"]: e for e in (strength_summary(db, this) or {}).get("exercises", [])}
        theirs = {e["exercise"]: e for e in (strength_summary(db, other) or {}).get("exercises", [])}
        for name in [*mine, *[n for n in theirs if n not in mine]]:
            exercises.append({"exercise": name, "this": mine.get(name), "other": theirs.get(name)})
    return {
        "candidates": candidates,
        "other": {**other, "id": other_id},
        "rows": out_rows,
        "zones": zones if zones["this"] or zones["other"] else None,
        "exercises": exercises,
        "similar_rule": "same sport and indoor/outdoor"
        + ("" if this["sport_type"] == "strength" else ", duration ±25%")
        + f", last {COMPARE_DAYS} days",
    }
