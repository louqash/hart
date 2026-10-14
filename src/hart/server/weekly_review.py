"""Weekly review: Ember's careful look at the week against the plans.

Code gathers and computes the facts — the plan against what was done, the weekly targets in the notes and
how often they were missed, six weeks of trend, grades, load, recovery, the season calendar and the race,
the previous review (the arithmetic is in ``analytics.weekly_review``). Claude — the strongest model at
extra-high effort by default, since nobody waits for it — reviews the week with the read tools, checks
what it claims, and may propose season changes (a longer base, a later build) or notes, which the athlete
applies. Code verifies every cited number (one repair round when too many can't be found), stores the
review for the Season page and posts it to Discord.

Scheduled weekly (Settings → Schedule). It waits for a Garmin sync after the scheduled time, so the
week's last sessions are in, and runs anyway a few hours later when Garmin is down.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, ValidationError

from hart.analytics import weekly_review as wr
from hart.config import HartSettings
from hart.server import settings, state
from hart.server.claude.policy import review_policy
from hart.server.claude.prompts import athlete_profile, coach_notes, data_notes
from hart.server.claude.runner import ClaudeRunner, RunOutcome, RunSpec
from hart.server.data import DAY, TRAINING_SPORTS, local_today, one, per_sport_ctl, readiness_on, recent_sessions, rows
from hart.server.grading import (
    UNVERIFIED_LIMIT,
    Citation,
    _structured_raw,
    claude_paused,
    pause_claude,
    verify_citations,
)
from hart.storage.database import Database

logger = logging.getLogger(__name__)

REVIEW_PROMPT_VERSION = "weekly_review@2"
# When the review model's safety classifiers decline the request, this model writes the review instead.
FALLBACK_MODEL = "claude-sonnet-5"
FALLBACK_EFFORT = "high"
TREND_WEEKS = 6
SEASON_MAX_WEEKS = 26  # a season review goes back to the comeback (or the phase start), at most this far
SEASON_FALLBACK_WEEKS = 12
BEFORE_LAYOFF_WEEKS = 8
SCOPES = ("week", "season")
MAX_TURNS = {"week": 40, "season": 60}
TIMEOUT_S = {"week": 1500, "season": 2400}
MAX_FAILURES = 2  # failed runs for a week before the schedule gives up on it
SYNC_LEAD = datetime.timedelta(hours=1)  # a sync this long before the scheduled time still counts as fresh
WAIT_FOR_SYNC = datetime.timedelta(hours=3)  # then it runs without one (Garmin down)
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

Verdict = Literal["on_track", "ahead", "mixed", "behind", "disrupted"]
Point = Annotated[str, Field(max_length=300)]


class ReviewOutput(BaseModel):
    analysis: str = Field(
        max_length=4000,
        description=(
            "Write this first: your working — plan vs done, targets and patterns, quality, load and recovery, "
            "the season — with the checks you made and what they showed."
        ),
    )
    verdict: Verdict
    headline: str = Field(max_length=200, description="One sentence.")
    summary: str = Field(max_length=1000, description="At most 4 sentences, the most important thing first.")
    went_well: list[Point] = Field(default_factory=list, max_length=4)
    missing: list[Point] = Field(
        default_factory=list, max_length=4, description="Missed or short against the plan and the targets, and why."
    )
    season: str = Field(
        max_length=1200,
        description="Is the current phase doing its job and does the calendar still fit the A-race; what you proposed.",
    )
    next_week: list[Point] = Field(default_factory=list, max_length=4)
    confidence: Literal["high", "medium", "low"]
    citations: list[Citation] = Field(default_factory=list, max_length=25)


_SYSTEM = """\
You write {name}'s weekly review: where they stand against their plans, what they did, what is \
missing, and whether the season plan still fits. {name} reads it on the Season page and in Discord. \
Nobody is waiting for it, so take the time to get it right: read carefully, check before you claim, \
and say "the data doesn't show" rather than guess.

You get a JSON bundle computed by code from their data:
- `week`: the dates reviewed. `complete` false means the week isn't over (`days_left` still to come): \
judge it so far, and never call a session missed before its day.
- `plan_adherence`: every session that stood in the plan (from the coach, the athlete or an accepted \
suggestion) with `status` done / missed / today / upcoming, sessions done without a plan \
(`unplanned`), and counts per sport and in total. Code matched sessions to the plan by date and sport; \
trust its counts.
- `targets`: weekly minimums from the notes (`min_sessions_per_week`): done, `short_by`, \
`short_streak_weeks` (weeks in a row short) and how many recent weeks fell short. One short week is not \
a pattern; three in a row is.
- `trend`: the last {weeks} weeks — sessions, minutes and load per sport, plan completion, grade \
average — and `volume_change` (this week against the weeks before).
- `sessions`: every session of the week with its grade (summary, concerns), RPE, feel and the \
athlete's note.
- `grades`; `load` (CTL/ATL/TSB going into the week and at its end, four weeks earlier, per sport); \
`recovery` (readiness, sleep, HRV and resting HR per day, and against the four weeks before); \
`annotations` (illness, injury, travel…); `anomalies`.
- `season`: the current phase (week n of m) and its goal, the observed state from the load curve and \
flags when they disagree, every phase from a month ago on (with ids), the races and the countdown to \
the A-race.
- `notes` (the source of truth about {name}), `pending_proposals` (changes already waiting — don't \
propose them again), `previous_review` (what was said last time: check whether it happened), \
`next_week_plan`, `data` (when Garmin last synced).
- `season_so_far` — only in a season review (`scope` "season": the first review, or when {name} asks \
for one): every week since the season started (the return from a layoff, or the current phase) and \
the weekly targets across them, and `before_layoff`, the weeks before the break, for where {name} \
stood. Then go over the whole season, not only this week: how the comeback and each phase so far have \
gone, the patterns across all the weeks, how far back toward the old level {name} is, and whether the \
calendar still fits the A-race. `analysis` and `season` are fuller; the rest stays as short.

# How to review
1. Read the notes first: goals, injuries, constraints and the weekly structure they set.
2. Plan vs done: what was done as planned, what was missed, what was done instead. Missed because of \
illness, injury, travel or a deliberate easy week (annotations, notes, the athlete's comments) is not \
the same as missed for no reason — say which.
3. Targets and patterns: is anything short again and again (e.g. too few runs)? Is the target \
realistic for the phase and the notes?
4. Quality: what the grades, RPE, feel and notes say about execution, how the body responded, and \
recurring concerns.
5. Load and recovery: did the load move the way the phase wants (building in base and build, easing in \
taper), and did recovery keep up?
6. Season: is the current phase doing its job, and does the calendar still fit the A-race? A longer \
base when the aerobic base clearly isn't there yet, a shorter one when it is ahead, an extra recovery \
week — or no change.
7. Next week: 2–4 concrete points, set against the plan that's there.

# Checking
- Use the hart tools whenever the bundle doesn't settle something: `get_session_grade` for a grade's \
details, `get_activity_detail` for a session, `get_planned_sessions`, `get_training_load`, \
`get_hrv_trend`, `run_sql_query` (after `describe_schema`). Before you call something a pattern, look \
at the weeks behind it. Blood work and health checks are not part of this review — leave them out.
- Every number you write must come from the bundle or a tool result in this run. List each in \
`citations` with its exact value and source ("facts.targets…" or the tool's name). Code checks them, \
and a review with numbers it can't find is sent back. Never estimate.
- Gaps in health data usually mean the watch wasn't worn — not a fault, not a data error.
- {data_notes}

# Changes
- Season calendar: when the evidence spans weeks, not one bad week, propose the change with \
`propose_season_change` and a clear reason tied to the data. To move a boundary between phases \
(lengthen base and start build later), use kind "phases" with every change in `changes`; they are \
applied together. Keep the race and the taper where they are. Nothing changes until {name} applies it.
- Notes: when the week shows something durable the notes lack or get wrong (a weekly target, a \
constraint that has ended), use `propose_athlete_note` or `propose_note_change`.
- Not the training plan. {coach_notes} Next week's points are advice; the daily suggestion turns them \
into sessions.
- Proposing nothing is fine and common. Say in `season` what you proposed and why.

# Output
- verdict: on_track, ahead, mixed, behind, or disrupted (illness, injury or travel took the week).
- headline: one sentence. summary: at most 4 sentences, the most important thing first.
- went_well, missing, next_week: short, specific points.
- Plain English, direct, no filler.
"""


def system_prompt(profile: dict[str, Any]) -> str:
    return _SYSTEM.format(
        name=profile["name"], weeks=TREND_WEEKS, data_notes=data_notes(profile), coach_notes=coach_notes(profile)
    )


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


def _decode_json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _latest_grades(db: Database, activity_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not activity_ids:
        return {}
    found = rows(
        db,
        "SELECT activity_id, letter, overall_score, confidence, session_type, summary, concerns FROM ("
        "SELECT *, row_number() OVER (PARTITION BY activity_id ORDER BY version DESC) AS rn FROM session_grades "
        f"WHERE status = 'graded' AND activity_id IN ({', '.join('?' for _ in activity_ids)})) WHERE rn = 1",
        activity_ids,
    )
    return {g["activity_id"]: {**g, "concerns": _decode_json(g["concerns"]) or []} for g in found}


def week_sessions(db: Database, start: datetime.date, end: datetime.date) -> list[dict[str, Any]]:
    """Every session of the week with its grade, feedback and (for strength) the exercises."""
    from hart.analytics.grading_features import strength_summary

    found = sorted(recent_sessions(db, start, end), key=lambda s: s["start_time"])
    grades = _latest_grades(db, [s["activity_id"] for s in found])
    out = []
    for s in found:
        zones = s.get("hr_zone_seconds") or {}
        total = sum(v for v in zones.values() if v) if isinstance(zones, dict) else 0
        g = grades.get(s["activity_id"])
        item: dict[str, Any] = {
            "activity_id": s["activity_id"],
            "date": s["date"],
            "weekday": s["date"].strftime("%a"),
            "sport": s["sport_type"],
            "indoor": s["indoor"],
            "name": s["name"],
            "minutes": round((s["duration_s"] or 0) / 60),
            "distance_km": round(s["distance_meters"] / 1000, 2) if s.get("distance_meters") else None,
            "load": round(s["load"]) if s.get("load") is not None else None,
            "avg_hr": s.get("avg_hr"),
            "hr_zones_pct": {z: round(100 * v / total) for z, v in sorted(zones.items()) if v} if total else None,
            "my_rpe": s.get("rpe"),
            "my_feel": s.get("feel"),
            "note": s.get("note"),
            "grade": {k: g[k] for k in ("letter", "overall_score", "session_type", "confidence", "summary", "concerns")}
            if g
            else None,
        }
        if s["sport_type"] == "strength":
            summary = strength_summary(db, s) or {}
            item["exercises"] = [
                {"exercise": e["exercise"], "sets": e["sets"], "top_kg": e["top_kg"], "top_reps": e["top_reps"]}
                for e in summary.get("exercises", [])
            ]
        out.append({k: v for k, v in item.items() if v is not None})
    return out


def standing_plan(db: Database, start: datetime.date, end: datetime.date) -> list[dict[str, Any]]:
    """The plan that stood each day: coach or athlete rows not replaced, plus accepted suggestions."""
    from hart.server.plan import plan_rows

    return [r for r in plan_rows(db, start, end) if r["replaced_by"] is None and r["sport_type"] != "rest"]


def weekly_rows(
    db: Database, week_start: datetime.date, today: datetime.date, weeks: int = TREND_WEEKS
) -> list[dict[str, Any]]:
    """*weeks* weeks ending with *week_start*'s (oldest first): training sessions, minutes and load per sport,
    plan completion and the grade average. Walks and other activities are left out."""
    first = week_start - (weeks - 1) * 7 * DAY
    end = week_start + 6 * DAY
    acts = rows(
        db,
        "SELECT a.activity_id, CAST(date_trunc('week', a.start_time) AS DATE) AS week, a.sport_type, "
        "coalesce(a.moving_seconds, a.elapsed_seconds) AS secs, m.tss AS load "
        "FROM activities a LEFT JOIN activity_metrics m USING (activity_id) "
        f"WHERE a.start_time >= ? AND a.start_time < ? AND a.sport_type IN ({', '.join('?' for _ in TRAINING_SPORTS)})",
        [first, end + DAY, *TRAINING_SPORTS],
    )
    plan = standing_plan(db, first, end)
    grades = _latest_grades(db, [a["activity_id"] for a in acts])
    out = []
    for i in range(weeks):
        ws = first + 7 * i * DAY
        mine = [a for a in acts if a["week"] == ws]
        by_sport: dict[str, dict[str, Any]] = {}
        for a in mine:
            sport = by_sport.setdefault(a["sport_type"], {"sessions": 0, "minutes": 0, "load": 0})
            sport["sessions"] += 1
            sport["minutes"] += round((a["secs"] or 0) / 60)
            sport["load"] += round(a["load"] or 0)
        due = [p for p in plan if ws <= p["date"] <= ws + 6 * DAY and (p["date"] < today or p["activity_id"])]
        done = sum(1 for p in due if p["activity_id"])
        scores = [grades[a["activity_id"]]["overall_score"] for a in mine if a["activity_id"] in grades]
        out.append(
            {
                "week_of": ws,
                "sessions": len(mine),
                "minutes": sum(s["minutes"] for s in by_sport.values()),
                "load": sum(s["load"] for s in by_sport.values()),
                "by_sport": by_sport,
                "plan_due": len(due),
                "plan_done": done,
                "plan_completion_pct": round(100 * done / len(due)) if due else None,
                "grade_average": round(sum(scores) / len(scores), 2) if scores else None,
            }
        )
    return out


def _load_on(db: Database, d: datetime.date) -> dict[str, Any] | None:
    return one(
        db,
        "SELECT date, round(ctl, 1) AS ctl, round(atl, 1) AS atl, round(tsb, 1) AS tsb FROM daily_training_load "
        "WHERE sport_type = 'combined' AND date <= ? ORDER BY date DESC LIMIT 1",
        [d],
    )


def load_facts(db: Database, start: datetime.date, through: datetime.date) -> dict[str, Any]:
    before, after = _load_on(db, start - DAY), _load_on(db, through)
    return {
        "going_into_the_week": before,
        "end_of_week": after,
        "ctl_change_this_week": round(after["ctl"] - before["ctl"], 1)
        if before and after and before["ctl"] is not None and after["ctl"] is not None
        else None,
        "four_weeks_earlier": _load_on(db, through - 28 * DAY),
        "per_sport_ctl": per_sport_ctl(db, through),
    }


def recovery_facts(db: Database, start: datetime.date, through: datetime.date, t: dict[str, float]) -> dict[str, Any]:
    days = []
    d = start
    while d <= through:
        readiness = readiness_on(db, d, t)
        sleep = one(db, "SELECT total_sleep_sec, sleep_score FROM sleep_records WHERE date = ?", [d]) or {}
        hrv = one(db, "SELECT hrv_last_night_ms FROM hrv_daily WHERE date = ?", [d]) or {}
        health = one(db, "SELECT resting_hr FROM daily_health WHERE date = ?", [d]) or {}
        day = {
            "date": d,
            "weekday": d.strftime("%a"),
            "readiness": readiness["level"],
            "readiness_reason": readiness.get("reason"),
            "sleep_h": round(sleep["total_sleep_sec"] / 3600, 1) if sleep.get("total_sleep_sec") else None,
            "sleep_score": sleep.get("sleep_score"),
            "hrv_ms": hrv.get("hrv_last_night_ms"),
            "resting_hr": health.get("resting_hr"),
        }
        days.append({k: v for k, v in day.items() if v is not None})
        d += DAY

    def averages(since: datetime.date, until: datetime.date) -> dict[str, Any]:
        found = one(
            db,
            "SELECT (SELECT round(avg(total_sleep_sec) / 3600, 2) FROM sleep_records WHERE date BETWEEN ? AND ?) "
            "AS sleep_h, (SELECT round(avg(hrv_last_night_ms), 1) FROM hrv_daily WHERE date BETWEEN ? AND ?) "
            "AS hrv_ms, (SELECT round(avg(resting_hr), 1) FROM daily_health WHERE date BETWEEN ? AND ?) "
            "AS resting_hr",
            [since, until] * 3,
        )
        return found or {}

    return {
        "days": days,
        "average_this_week": averages(start, through),
        "average_4_weeks_before": averages(start - 28 * DAY, start - DAY),
    }


def season_facts(
    db: Database, start: datetime.date, through: datetime.date, t: dict[str, float], phases_since: datetime.date
) -> dict[str, Any]:
    from hart.server.data import season_context

    ctx = season_context(db, through, t)
    phases = rows(
        db,
        "SELECT id, phase_type, name, start_date, end_date, goal, source, confirmed FROM training_phases "
        "WHERE end_date >= ? ORDER BY start_date",
        [phases_since],
    )
    for p in phases:
        p["length_days"] = (p["end_date"] - p["start_date"]).days + 1
    races = rows(
        db, "SELECT id, name, race_date, distance, priority FROM races WHERE race_date >= ? ORDER BY race_date", [start]
    )
    a_race = next((r for r in races if r["priority"] == "A"), None)
    countdown = None
    if a_race:
        days = (a_race["race_date"] - through).days
        countdown = {"race": a_race["name"], "date": a_race["race_date"], "days": days, "weeks": round(days / 7, 1)}
    return {
        "current_phase": ctx["phase"],
        "observed_state": ctx["state"],
        "flags": ctx["flags"],
        "phases": phases,
        "races": races,
        "a_race_countdown": countdown,
        "layoff_detection": state.get_setting(db, "season_detection"),
    }


def proposal_states(db: Database, proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each proposal from a review with where it stands now: pending, applied or dismissed."""
    out = []
    for p in proposals or []:
        if p.get("type") == "season":
            found = one(db, "SELECT status FROM season_proposals WHERE id = ?", [p["id"]])
            status = found["status"] if found else "gone"
        else:
            found = one(db, "SELECT status FROM athlete_notes WHERE id = ?", [p["id"]])
            status = {"proposed": "pending", "active": "applied", "archived": "dismissed"}.get(
                (found or {}).get("status"), "decided"
            )
        out.append({**p, "status": status})
    return out


def previous_review(db: Database, week_start: datetime.date) -> dict[str, Any] | None:
    found = one(
        db,
        "SELECT week_start, made_on, verdict, headline, review, proposals FROM weekly_reviews "
        "WHERE status = 'ok' AND week_start < ? ORDER BY week_start DESC, version DESC LIMIT 1",
        [week_start],
    )
    if not found:
        return None
    review = _decode_json(found["review"]) or {}
    return {
        "week_of": found["week_start"],
        "made_on": found["made_on"],
        "verdict": found["verdict"],
        "headline": found["headline"],
        "missing": review.get("missing"),
        "next_week": review.get("next_week"),
        "season": review.get("season"),
        "proposals": proposal_states(db, _decode_json(found["proposals"]) or []),
    }


def season_start(db: Database, week_start: datetime.date) -> datetime.date:
    """Where a season review starts: the return from the detected layoff, else the start of the current
    phase, else SEASON_FALLBACK_WEEKS back."""
    from hart.server.data import phase_on

    layoff = (state.get_setting(db, "season_detection") or {}).get("layoff") or {}
    if layoff.get("return_date"):
        return datetime.date.fromisoformat(layoff["return_date"])
    phase = phase_on(db, week_start)
    return phase["start_date"] if phase else week_start - SEASON_FALLBACK_WEEKS * 7 * DAY


def season_so_far(
    db: Database, week_start: datetime.date, today: datetime.date, targets: dict[str, int], left: int
) -> dict[str, Any]:
    """For a season review: every week since the season started (the comeback), the weekly targets across
    them, and — after a layoff — the weeks before the break, for where the athlete stood."""
    since = season_start(db, week_start)
    weeks = weekly_rows(db, week_start, today, wr.season_weeks(week_start, since, TREND_WEEKS, SEASON_MAX_WEEKS))
    out: dict[str, Any] = {
        "since": since,
        "weeks": weeks,
        "targets_across_weeks": wr.target_status(
            targets, [{s: v["sessions"] for s, v in w["by_sport"].items()} for w in weeks], left
        ),
    }
    layoff = (state.get_setting(db, "season_detection") or {}).get("layoff") or {}
    if layoff.get("last_activity_date"):
        last = datetime.date.fromisoformat(layoff["last_activity_date"])
        out["before_layoff"] = {
            "last_session_before_the_break": last,
            "break_days": layoff.get("gap_days"),
            "weeks": weekly_rows(db, last - last.weekday() * DAY, today, BEFORE_LAYOFF_WEEKS),
        }
    return out


def build_context(db: Database, week_start: datetime.date, today: datetime.date, scope: str = "week") -> dict[str, Any]:
    from hart.server.jobs.scheduler import last_successful_sync
    from hart.server.suggestions import _merged_rule, active_notes

    t = state.get_thresholds(db)
    week_end = week_start + 6 * DAY
    through = min(week_end, today)
    left = wr.days_left(week_end, today)
    sessions = week_sessions(db, week_start, week_end)
    adherence = wr.plan_adherence(
        standing_plan(db, week_start, week_end),
        [
            {"activity_id": s["activity_id"], "date": s["date"], "sport_type": s["sport"], "minutes": s["minutes"]}
            for s in sessions
        ],
        today,
    )
    trend = weekly_rows(db, week_start, today)
    notes = active_notes(db, through)
    target_rules = {s: int(v) for s, v in _merged_rule(notes, "min_sessions_per_week").items()}
    targets = wr.target_status(
        target_rules, [{s: v["sessions"] for s, v in w["by_sport"].items()} for w in trend], left
    )
    season_review = season_so_far(db, week_start, today, target_rules, left) if scope == "season" else None
    grades = wr.grade_summary(
        [
            {**s["grade"], "activity_id": s["activity_id"], "date": s["date"], "sport": s["sport"]}
            for s in sessions
            if s.get("grade")
        ]
    )
    this_week = trend[-1]
    last_sync = last_successful_sync(db)
    next_start = week_end + DAY
    phases_since = season_review["since"] - 28 * DAY if season_review else week_start - 28 * DAY
    return {
        "scope": scope,
        "week": {
            "start": week_start,
            "end": week_end,
            "made_on": today,
            "complete": today > week_end,
            "days_left": left,
        },
        **({"season_so_far": season_review} if season_review else {}),
        "scorecard": {
            **wr.scorecard(adherence, targets, grades),
            "hours": round(this_week["minutes"] / 60, 1),
            "load": this_week["load"],
        },
        "plan_adherence": adherence,
        "targets": targets,
        "trend": {"weeks": trend, "volume_change": wr.volume_change(trend)},
        "sessions": sessions,
        "grades": grades,
        "load": load_facts(db, week_start, through),
        "recovery": recovery_facts(db, week_start, through, t),
        "annotations": rows(
            db,
            "SELECT id, kind, label, start_date, end_date FROM annotations WHERE start_date <= ? "
            "AND coalesce(end_date, ?) >= ? ORDER BY start_date",
            [week_end, week_end, week_start],
        ),
        "anomalies": rows(
            db,
            "SELECT CAST(detected_at AS DATE) AS date, severity, sport_type, metric_name, description, acknowledged "
            "FROM anomaly_log WHERE CAST(detected_at AS DATE) BETWEEN ? AND ? ORDER BY detected_at LIMIT 20",
            [week_start, week_end],
        ),
        "season": season_facts(db, week_start, through, t, phases_since),
        "notes": [{k: n.get(k) for k in ("id", "category", "title", "body", "rules")} for n in notes],
        "pending_proposals": {
            "season": rows(
                db,
                "SELECT id, kind, summary, reason, CAST(created_at AS DATE) AS proposed_on FROM season_proposals "
                "WHERE status = 'pending' ORDER BY id",
            ),
            "notes": rows(
                db,
                "SELECT id, category, title, proposed_action, target_id FROM athlete_notes "
                "WHERE status = 'proposed' ORDER BY id",
            ),
        },
        "previous_review": previous_review(db, week_start),
        "next_week_plan": [
            {
                "date": p["date"],
                "weekday": p["date"].strftime("%a"),
                "sport": p["sport_type"],
                "title": p["title"],
                "duration_min": p["duration_min"],
                "intensity": p["intensity"],
                "from": wr.PLAN_FROM.get(p["source"], p["source"]),
            }
            for p in standing_plan(db, next_start, next_start + 6 * DAY)
        ],
        "data": {"last_garmin_sync": last_sync},
    }


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


class ReviewPaused(Exception):
    pass


PROPOSAL_TOOLS = {"propose_season_change": "season", "propose_athlete_note": "note", "propose_note_change": "note"}


def proposals_from(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The changes Ember proposed during the run, from its successful proposal tool calls."""
    out = []
    for call in calls:
        kind = PROPOSAL_TOOLS.get(call.get("name") or "")
        if kind is None or call.get("is_error"):
            continue
        try:
            result = json.loads(call.get("output_excerpt") or "")
        except ValueError:
            continue
        if not isinstance(result, dict) or result.get("error") or not result.get("id"):
            continue
        given = call.get("input") or {}
        if call["name"] == "propose_athlete_note":
            summary = f"New note: {given.get('title')}"
        elif call["name"] == "propose_note_change":
            summary = f"{'Archive' if given.get('action') == 'archive' else 'Change'} note #{given.get('note_id')}"
            summary += f": {given['title']}" if given.get("title") else ""
        else:
            summary = result.get("summary") or "Season change"
        out.append({"type": kind, "id": result["id"], "summary": summary, "reason": given.get("reason")})
    return out


def _parse(outcome: RunOutcome) -> tuple[ReviewOutput | None, str | None]:
    if outcome.status != "ok":
        return None, outcome.error
    raw = _structured_raw(outcome)
    if raw is None:
        return None, "no structured output"
    try:
        return ReviewOutput.model_validate(raw), None
    except ValidationError as exc:
        return None, str(exc)[:800]


def _unverified(citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    missing = [c for c in citations if not c["verified"]]
    return missing if citations and len(missing) / len(citations) > UNVERIFIED_LIMIT else []


def _next_version(db: Database, week_start: datetime.date) -> int:
    return db.fetchone("SELECT coalesce(max(version), 0) + 1 FROM weekly_reviews WHERE week_start = ?", [week_start])[0]


def store(db: Database, week_start: datetime.date, fields: dict[str, Any]) -> int:
    cols = ["week_start", "version", *fields]
    values = [
        week_start,
        _next_version(db, week_start),
        *[json.dumps(v, default=str) if isinstance(v, dict | list) else v for v in fields.values()],
    ]
    return db.fetchone(
        f"INSERT INTO weekly_reviews ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) RETURNING id",
        values,
    )[0]


def store_failure(
    db: Database, week_start: datetime.date, today: datetime.date, trigger: str, error: str, run_id: int | None = None
) -> int:
    return store(
        db,
        week_start,
        {"made_on": today, "trigger": trigger, "status": "failed", "error": error[:1000], "claude_run_id": run_id},
    )


def default_scope(db: Database) -> str:
    """The first review goes over the whole season so far; after that, the week."""
    return "week" if db.fetchone("SELECT 1 FROM weekly_reviews WHERE status = 'ok' LIMIT 1") else "season"


def generate(
    db: Database,
    week_start: datetime.date,
    claude: ClaudeRunner,
    loop: asyncio.AbstractEventLoop,
    config: HartSettings,
    *,
    trigger: str = "manual",
    scope: str | None = None,
) -> dict[str, Any]:
    """Review one week and store it (runs in a job thread; Claude runs on the event loop). The first review —
    or one asked for with scope "season" — also goes over the whole season so far."""
    paused = claude_paused(db)
    if paused:
        raise ReviewPaused(f"Claude paused until {paused}")
    scope = scope if scope in SCOPES else default_scope(db)
    today = local_today(config)
    bundle = build_context(db, week_start, today, scope)
    bundle["trigger"] = trigger
    facts = json.dumps(bundle, default=str, indent=1)
    week_end = week_start + 6 * DAY
    so_far = "" if bundle["week"]["complete"] else " — the week so far"
    task = f"Review the week {week_start:%a %d %b} – {week_end:%a %d %b %Y}{so_far}"
    if scope == "season":
        task += f", and the whole season since {bundle['season_so_far']['since']:%d %b %Y} (`season_so_far`)"
    prompt = f"{task}. Facts:\n```json\n{facts}\n```"
    spec = RunSpec(
        purpose="weekly_review",
        prompt=prompt,
        model=settings.get(db, "model_weekly_review"),
        system_prompt=system_prompt(athlete_profile(db)),
        prompt_version=REVIEW_PROMPT_VERSION,
        policy=review_policy(),
        max_turns=MAX_TURNS[scope],
        timeout_s=TIMEOUT_S[scope],
        background=True,
        output_schema=ReviewOutput.model_json_schema(),
        effort=settings.get(db, "weekly_review_effort"),
    )

    def run(s: RunSpec) -> RunOutcome:
        outcome = asyncio.run_coroutine_threadsafe(claude.run(s), loop).result(timeout=s.timeout_s + 60)
        if outcome.status in ("usage_limited", "auth_failed"):
            pause_claude(db, outcome)
            raise ReviewPaused(outcome.error or outcome.status)
        return outcome

    outcome = run(spec)
    sources, calls = [facts, *outcome.tool_outputs], list(outcome.tool_calls)
    declined_by = None
    if outcome.status == "refused" and spec.model != FALLBACK_MODEL:
        # A safety classifier declined it (false positives on health data happen) — the same input would be
        # declined again, so another model writes the review instead.
        logger.warning("Weekly review: %s declined (%s); retrying with %s", spec.model, outcome.error, FALLBACK_MODEL)
        declined_by = spec.model
        spec = RunSpec(**{**spec.__dict__, "model": FALLBACK_MODEL, "effort": FALLBACK_EFFORT})
        outcome = run(spec)
        sources += outcome.tool_outputs
        calls += outcome.tool_calls
    output, error = _parse(outcome)
    citations = verify_citations(output.citations, sources) if output else []
    unverified = _unverified(citations)
    if (output is None and outcome.status == "ok") or unverified:
        if unverified:
            listed = "; ".join(f"{c['label']} = {c['value']} ({c['source']})" for c in unverified)
            problem = (
                f"these numbers weren't found in the facts or in any tool result: {listed}. Check each one "
                "against the bundle or a tool, correct it or leave it out"
            )
        else:
            problem = f"it failed validation: {error}"
        repair = RunSpec(
            **{
                **spec.__dict__,
                "resume": outcome.session_id,
                "prompt": f"Your review can't be stored yet: {problem}. Return the whole review again.",
            }
        )
        outcome = run(repair)
        sources += outcome.tool_outputs
        calls += outcome.tool_calls
        repaired, repair_error = _parse(outcome)
        if repaired is not None:
            output, error = repaired, None
            citations = verify_citations(output.citations, sources)
        elif output is None:
            error = repair_error

    proposals = proposals_from(calls)
    if output is None:
        error = error or outcome.error or "no output"
        if declined_by:
            error = f"{declined_by} declined the review (safeguards); {spec.model} then failed too: {error}"
        review_id = store_failure(db, week_start, today, trigger, error, outcome.run_id)
        return {"status": "failed", "review_id": review_id, "error": error}

    confidence = "low" if _unverified(citations) else output.confidence
    review_id = store(
        db,
        week_start,
        {
            "made_on": today,
            "trigger": trigger,
            "status": "ok",
            "verdict": output.verdict,
            "headline": output.headline,
            "summary": output.summary,
            "review": {
                "analysis": output.analysis,
                "went_well": output.went_well,
                "missing": output.missing,
                "season": output.season,
                "next_week": output.next_week,
                "model": spec.model,
                "declined_by": declined_by,
            },
            "confidence": confidence,
            "citations": citations,
            "proposals": proposals,
            "context": bundle,
            "claude_run_id": outcome.run_id,
        },
    )
    discord = send(db, config, review_id)
    db.execute("UPDATE weekly_reviews SET discord = ? WHERE id = ?", [discord, review_id])
    return {
        "status": "ok",
        "review_id": review_id,
        "verdict": output.verdict,
        "proposals": len(proposals),
        "discord": discord,
    }


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def scheduled_at(db: Database, now_local: datetime.datetime) -> datetime.datetime | None:
    """The latest scheduled review time at or before *now_local*; None when the review is off."""
    day = settings.get(db, "weekly_review_day")
    if day not in WEEKDAYS:
        return None
    back = (now_local.weekday() - WEEKDAYS.index(day)) % 7
    slot = datetime.datetime.combine(
        now_local.date() - back * DAY, settings.get_time(db, "weekly_review_time"), now_local.tzinfo
    )
    return slot if slot <= now_local else slot - 7 * DAY


def pending(db: Database, week_start: datetime.date) -> bool:
    return (
        db.fetchone(
            "SELECT 1 FROM jobs WHERE dedupe_key = ? AND status IN ('queued', 'running')",
            [f"weekly_review:{week_start}"],
        )
        is not None
    )


def due(db: Database, now_local: datetime.datetime) -> datetime.date | None:
    """The week to review now, or None.

    Due from the scheduled time until that week has a review made on the scheduled day or later (one made
    earlier in the week covers less), and not after MAX_FAILURES failed runs or with no training at all in
    the trend window. It waits for a Garmin sync from about the scheduled time on (the week's last
    sessions), but no longer than WAIT_FOR_SYNC. A server that was down at the scheduled time catches up
    once it's back."""
    from hart.server.jobs.scheduler import last_successful_sync

    slot = scheduled_at(db, now_local)
    if slot is None or claude_paused(db):
        return None
    week_start, _ = wr.review_week(slot.date())
    made = [
        r[0]
        for r in db.fetchall(
            "SELECT status FROM weekly_reviews WHERE week_start = ? AND made_on >= ?", [week_start, slot.date()]
        )
    ]
    if "ok" in made or made.count("failed") >= MAX_FAILURES or pending(db, week_start):
        return None
    trend_start = week_start - (TREND_WEEKS - 1) * 7 * DAY
    if db.fetchone("SELECT 1 FROM activities WHERE start_time >= ? LIMIT 1", [trend_start]) is None:
        return None  # nothing to review (a new install, or weeks without the watch)
    last = last_successful_sync(db)
    if (last is None or last < slot - SYNC_LEAD) and now_local < slot + WAIT_FOR_SYNC:
        return None
    return week_start


def schedule_label(db: Database) -> str | None:
    day = settings.get(db, "weekly_review_day")
    if day not in WEEKDAYS:
        return None
    return f"{settings.WEEKDAY_CHOICES[day]}s from {settings.get(db, 'weekly_review_time')}"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

COLUMNS = (
    "id, week_start, version, made_on, trigger, status, verdict, headline, summary, review, confidence, citations, "
    "proposals, error, discord, claude_run_id, created_at"
)
VERDICT_LABELS = {
    "on_track": "On track",
    "ahead": "Ahead of plan",
    "mixed": "Mixed",
    "behind": "Behind",
    "disrupted": "Disrupted",
}


def _decode(db: Database, r: dict[str, Any]) -> dict[str, Any]:
    for key in ("review", "citations", "proposals", "scorecard", "season", "week"):
        if key in r:
            r[key] = _decode_json(r[key])
    r["proposals"] = proposal_states(db, r.get("proposals") or [])
    r["verdict_label"] = VERDICT_LABELS.get(r.get("verdict") or "", r.get("verdict"))
    r["week_end"] = r["week_start"] + 6 * DAY
    r["so_far"] = r["made_on"] <= r["week_end"]
    return r


def get(db: Database, review_id: int, *, with_context: bool = False) -> dict[str, Any] | None:
    extra = (
        ", context"
        if with_context
        else ", context->'scorecard' AS scorecard, context->'season' AS season, context->>'scope' AS scope"
    )
    found = one(db, f"SELECT {COLUMNS}{extra} FROM weekly_reviews WHERE id = ?", [review_id])
    if not found:
        return None
    if with_context:
        found["context"] = _decode_json(found["context"])
        found["scorecard"] = (found["context"] or {}).get("scorecard")
        found["season"] = (found["context"] or {}).get("season")
        found["scope"] = (found["context"] or {}).get("scope")
    return _decode(db, found)


def history(db: Database, limit: int = 26) -> list[dict[str, Any]]:
    """The latest review of each week, newest week first (a good one over a newer failure)."""
    found = rows(
        db,
        f"SELECT {COLUMNS}, context->'scorecard' AS scorecard, context->>'scope' AS scope FROM ("
        "SELECT *, row_number() OVER (PARTITION BY week_start ORDER BY status = 'ok' DESC, version DESC) AS rn "
        "FROM weekly_reviews) WHERE rn = 1 ORDER BY week_start DESC LIMIT ?",
        [limit],
    )
    return [_decode(db, r) for r in found]


def season_card(db: Database, config: HartSettings) -> dict[str, Any]:
    """The Season page's weekly review card: the latest review, the history and what a run now would cover."""
    today = local_today(config)
    week_start, week_end = wr.review_week(today)
    reviews = history(db)
    latest = get(db, reviews[0]["id"]) if reviews else None
    return {
        "latest": latest,
        "history": reviews,
        "week_to_review": week_start,
        "week_complete": week_end < today,
        "next_scope": default_scope(db),
        "pending": pending(db, week_start),
        "claude_paused": claude_paused(db),
        "schedule": schedule_label(db),
    }


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

VERDICT_STYLE = {  # embed colour (the app's palette) and label
    "on_track": (0x6EB886, "✅ On track"),
    "ahead": (0x9CC27A, "🚀 Ahead of plan"),
    "mixed": (0xE2A45F, "🌗 Mixed"),
    "behind": (0xE27D60, "⚠️ Behind"),
    "disrupted": (0x5B7564, "🌧️ Disrupted"),
}


def _bullets(items: list[str] | None, limit: int = 3) -> str:
    return "\n".join(f"• {x}" for x in (items or [])[:limit])


def scorecard_lines(card: dict[str, Any]) -> list[str]:
    from hart.server.evening import SPORT_ICONS

    lines = []
    for s in card.get("sports") or []:
        if not (s["sessions"] or s["planned_due"] or s["target"]):
            continue
        parts = [f"{SPORT_ICONS.get(s['sport'], '•')} {s['sport'].title()}"]
        if s["planned_due"]:
            parts.append(f"{s['done_as_planned']}/{s['planned_due']} planned")
        parts.append(f"{s['sessions']} done" if s["sessions"] != s["done_as_planned"] or not s["planned_due"] else "")
        if s["target"]:
            parts.append(f"target {s['target']} {'✓' if s['target_met'] else '✗'}")
        lines.append(" · ".join(p for p in parts if p))
    total = [f"{card.get('hours') or 0} h", f"load {card.get('load') or 0}"]
    if card.get("grade_average") is not None:
        total.append(f"grades avg {card['grade_average']:.1f}/5")
    lines.append(" · ".join(total))
    return lines


def build_message(db: Database, config: HartSettings, review: dict[str, Any]) -> dict[str, Any]:
    from hart.server.evening import _clip

    start, end = review["week_start"], review["week_end"]
    colour, label = VERDICT_STYLE.get(review["verdict"] or "", (0x5B7564, review["verdict"] or ""))
    kind = "Season review · week of" if review.get("scope") == "season" else "Week review ·"
    title = f"🧭 {kind} {start.day} {start:%b} – {end.day} {end:%b}" + (" (so far)" if review["so_far"] else "")
    if review["version"] > 1:
        title += " · updated"
    sections = review.get("review") or {}
    fields: list[dict[str, Any]] = [
        {"name": "📊 Plan vs done", "value": "\n".join(scorecard_lines(review.get("scorecard") or {}))}
    ]
    for name, key in (("👍 Went well", "went_well"), ("🕳️ Missing", "missing")):
        if sections.get(key):
            fields.append({"name": name, "value": _clip(_bullets(sections[key]), 1024)})
    if sections.get("season"):
        fields.append({"name": "🗺️ Season", "value": _clip(sections["season"], 1024)})
    if sections.get("next_week"):
        fields.append({"name": "➡️ Next week", "value": _clip(_bullets(sections["next_week"], 4), 1024)})
    waiting = [p for p in review.get("proposals") or [] if p["status"] == "pending"]
    if waiting:
        fields.append(
            {
                "name": "📝 Waiting for you",
                "value": _clip(_bullets([p["summary"] for p in waiting]) + "\nApply or dismiss them in hart.", 1024),
            }
        )
    embed: dict[str, Any] = {
        "title": _clip(title, 256),
        "description": _clip(f"**{label}** — {review['headline']}\n\n{review['summary']}", 4000),
        "color": colour,
        "fields": fields,
    }
    season = review.get("season") or {}
    footer = []
    phase = season.get("current_phase")
    if phase:
        footer.append(f"{phase['name']} · week {phase['week']}/{phase['weeks']}")
    countdown = season.get("a_race_countdown")
    if countdown and countdown["days"] >= 0:
        footer.append(f"{countdown['race']} in {countdown['days'] // 7} weeks")
    if review.get("confidence") == "low":
        footer.append("low confidence")
    if sections.get("declined_by"):
        footer.append(f"written by {sections.get('model')}")
    if footer:
        embed["footer"] = {"text": " · ".join(footer)}
    if config.server.public_host:
        embed["url"] = f"https://{config.server.public_host}/season#weekly-review"
    return {"embeds": [embed]}


def send(db: Database, config: HartSettings, review_id: int) -> str:
    """Post the review to Discord; never fails the run (the review is stored either way)."""
    from hart.server import evening

    if not evening.configured(config) or not settings.get(db, "weekly_review_message_enabled"):
        return "off"
    try:
        review = get(db, review_id)
        if review is None or review["status"] != "ok":
            return "skipped"
        evening.send(config, build_message(db, config, review))
        return "sent"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Discord weekly review %s failed: %s", review_id, exc)
        return f"failed: {exc}"[:200]
