"""Next-session suggestions.

Code builds the context bundle and the hard limits (``analytics.guardrails``);
Claude proposes a recommendation and sessions; code checks the answer against
the limits, repairs once, and stores it.  The coach's plan always takes
priority — the suggestion is framed as an adjustment to it.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError

from hart.analytics.guardrails import WEEKDAYS, GuardContext, check, limits
from hart.config import HartSettings
from hart.server import settings, state
from hart.server.claude.policy import read_only_policy
from hart.server.claude.prompts import athlete_profile
from hart.server.claude.runner import ClaudeRunner, RunOutcome, RunSpec
from hart.server.data import (
    DAY,
    TRAINING_SPORTS,
    local_today,
    monday,
    one,
    readiness_on,
    recent_sessions,
    rows,
    season_context,
)
from hart.server.grading import Citation, _structured_raw, claude_paused, pause_claude, verify_citations
from hart.server.plan import PlanRowIn, active_plan_for, coach_plan_for, create_row, garmin_rules
from hart.storage.database import Database

logger = logging.getLogger(__name__)

SUGGEST_PROMPT_VERSION = "suggest@9"
HISTORY_DAYS = 14
WEEKLY_TOTALS_WEEKS = 4
FINAL_CUTOFF_HOUR = 14
ACCEPTABLE = ("free_choice", "modify", "replace")

Recommendation = Literal["as_planned", "modify", "replace", "rest", "free_choice"]


class SuggestedSession(BaseModel):
    sport_type: Literal["swim", "bike", "run", "strength", "other"]
    title: str = Field(max_length=100)
    duration_min: int = Field(ge=5, le=480)
    intensity: Literal["recovery", "endurance", "tempo", "threshold", "vo2", "strength", "mixed"]
    structure: str | None = Field(
        default=None, max_length=1500, description="Steps, one per line (warm-up, main set, cool-down)"
    )
    garmin_text: str | None = Field(
        default=None, max_length=2500, description="Bike sessions with power steps only: Garmin workout text"
    )
    rationale: str = Field(max_length=500)


class SuggestionOutput(BaseModel):
    week_review: str = Field(
        max_length=900,
        description=(
            "Write this first. Go through training_history and week_targets against the weekly structure in the "
            "athlete notes: what was done, what was missed or is still owed this week, and what that means for "
            "the target date."
        ),
    )
    changed_from_previous: str | None = Field(
        default=None,
        max_length=400,
        description=("Only when previous_suggestion exists: what you changed and why (null if you kept it)."),
    )
    coach_stance: Literal["agree", "partly", "disagree"] | None = Field(
        default=None, description="Only when the coach planned the target date: do you agree with their session?"
    )
    coach_take: str | None = Field(
        default=None,
        max_length=600,
        description=(
            "Only when the coach planned the target date and you'd do something different: what, and why, from "
            "the data — plainly, as you'd tell a friend. Null when you agree."
        ),
    )
    recommendation: Recommendation
    sessions: list[SuggestedSession] = Field(default_factory=list, max_length=2)
    summary: str = Field(max_length=700)
    cautions: list[str] = Field(default_factory=list, max_length=4)
    citations: list[Citation] = Field(default_factory=list, max_length=12)


_SYSTEM = """\
You suggest the next training day for {name}, an endurance athlete. Their goals, injuries, \
constraints and weekly structure are in the athlete notes in the bundle (and `get_athlete_context`). \
{coach_notes} Your job is to adjust the plan to how {name} is today, or to propose something \
sensible on open days.

You get a JSON bundle computed from their data: readiness (with the inputs and rules that fired, \
and `steps`: yesterday on foot vs their usual — a `high` flag is a long walk or hike whose fatigue the \
training load doesn't count, `low` often means travel or illness), \
the coach's plan for the day (verbatim, if any), `standing_plan` (what is planned for the day right \
now — the coach's or the athlete's own sessions, and suggestions they already accepted), season phase \
and observed state, `training_history` \
(every session of the last 14 days: sport, duration, load, heart-rate zones, grade, their RPE/feel and \
their note on the session, \
and the exercises of strength sessions), `weekly_totals` for the last 4 weeks, this week's plan, \
`week_targets` (sessions per week the notes ask for, how many are done and still owed), active \
athlete notes, `limits` — hard limits that code enforces after you answer — and, when regenerating, \
`previous_suggestion` with `since_previous` (which inputs changed). You may call the read-only \
hart tools for more (e.g. `get_strength_history`, `get_hrv_trend`), but keep it to a few calls.

# Before suggesting
1. Review the week (`week_review`, written first): compare `training_history` with the weekly \
structure in the notes (e.g. two strength sessions per week, a fixed swim day) and \
`week_targets`. Name anything missed or still owed. A session the week still owes — above all one \
listed in `limits.required_today` — comes before an optional extra session.
2. Respect recovery spacing from the notes (e.g. ~48 h between a heavy leg session and a hard run).
3. If `previous_suggestion` exists: keep it unless `since_previous.changed` lists new facts or your \
week review shows it was wrong; if you change it, say what and why in `changed_from_previous`. \
Don't swap sports or durations for no reason.

# Recommendation
- `standing_plan` is what you work against. If it already holds what you'd suggest — including a \
suggestion the athlete accepted earlier — answer `as_planned` and repeat it; never offer the same \
session again as something new (accepting it would plan it twice).
- A plan stands for the day: `as_planned` (repeat the planned session in `sessions`, adding structure \
only if useful), `modify` (same session, shorter or easier, with concrete changes), `replace` (a different \
session), or `rest`. Never add duration or intensity beyond the plan — code enforces it.
- Think for yourself about the coach's session. The coach may not know what the data shows: a missed \
session the week still owes, recovery spacing, a niggle in the comments, how the last similar \
sessions went. Set `coach_stance` and, when you'd do something different, argue it in `coach_take` — \
honestly and specifically, even when you still recommend `as_planned` (e.g. "I'd move the intervals \
to Thursday: …"), and also when you'd do *more* than the plan (you can't prescribe more, but you can \
say so). `replace` needs your case in `coach_take`. Don't disagree for the sake of it: agreeing is \
fine and common. The athlete decides.
- No coach plan: `free_choice` with 1–2 sessions consistent with the phase, readiness, what's been \
done this week and the notes — or `rest`.
- If sessions were already done on the target date, don't prescribe the same work again; suggest \
what's left (or `rest`).
- Readiness red: no threshold, vo2 or mixed work. Readiness unknown: be conservative, and say which \
data is missing.
- Respect every limit in `limits` exactly (note rules, weekly session counts, comeback duration caps).

# Output
- sessions: sport, short title, duration, intensity, `structure` as simple steps, and a one-sentence \
rationale tied to the data. For a bike session with power steps also fill `garmin_text` following \
the Garmin workout formatter rules below; otherwise leave it null.
- summary: ≤ 3 sentences, plain English, direct. cautions: short, only real ones.
- Every number you mention must come from the bundle or a tool result; list each in `citations` \
with its exact value and source. Never estimate or invent numbers. {data_notes}

# Garmin workout formatter rules
{garmin_rules}
"""


def system_prompt(profile: dict[str, Any]) -> str:
    from hart.server.claude.prompts import coach_notes, data_notes

    # str.replace, not format: the Garmin rules contain braces.
    return (
        _SYSTEM.replace("{name}", profile["name"])
        .replace("{coach_notes}", coach_notes(profile))
        .replace("{data_notes}", data_notes(profile))
        .replace("{garmin_rules}", garmin_rules() or "(not available — leave garmin_text null)")
    )


# ---------------------------------------------------------------------------
# Context and limits
# ---------------------------------------------------------------------------


def active_notes(db: Database, d: datetime.date) -> list[dict[str, Any]]:
    notes = rows(
        db,
        "SELECT id, category, title, body, rules FROM athlete_notes WHERE status = 'active' "
        "AND (valid_from IS NULL OR valid_from <= ?) AND (valid_to IS NULL OR valid_to >= ?) ORDER BY category, id",
        [d, d],
    )
    for n in notes:
        if isinstance(n.get("rules"), str):
            n["rules"] = json.loads(n["rules"])
    return notes


def _event_days(db: Database, since: datetime.date, until: datetime.date) -> set[datetime.date]:
    days: set[datetime.date] = set()
    for a in rows(
        db,
        "SELECT start_date, end_date FROM annotations WHERE kind = 'event' AND start_date <= ? "
        "AND coalesce(end_date, ?) >= ?",
        [until, until, since],
    ):
        d = max(a["start_date"], since)
        while d <= min(a["end_date"] or until, until):
            days.add(d)
            d += DAY
    return days


def longest_recent(db: Database, d: datetime.date, days: int = 14) -> dict[str, int]:
    since = d - days * DAY
    events = _event_days(db, since, d)
    out: dict[str, int] = {}
    for r in rows(
        db,
        "SELECT sport_type, CAST(start_time AS DATE) AS day, coalesce(moving_seconds, elapsed_seconds) AS secs "
        "FROM activities WHERE start_time >= ? AND start_time < ?",
        [since, d],
    ):
        if r["day"] in events or r["sport_type"] not in TRAINING_SPORTS:
            continue
        out[r["sport_type"]] = max(out.get(r["sport_type"], 0), round((r["secs"] or 0) / 60))
    return out


def week_counts(db: Database, d: datetime.date) -> dict[str, int]:
    """Sessions per sport in d's week: done (any day) plus still-open plan
    rows on other days.  The target day's own plan is not counted — it is
    what the suggestion is about."""
    start = monday(d)
    counts: dict[str, int] = {}
    for r in db.fetchall(
        "SELECT sport_type, count(*) FROM activities WHERE start_time >= ? AND start_time < ? GROUP BY 1",
        [start, start + 7 * DAY],
    ):
        counts[r[0]] = counts.get(r[0], 0) + r[1]
    for r in db.fetchall(
        "SELECT p.sport_type, count(*) FROM planned_sessions p WHERE p.date >= ? AND p.date < ? AND p.date <> ? "
        "AND p.activity_id IS NULL AND p.sport_type <> 'rest' "
        "AND NOT EXISTS (SELECT 1 FROM planned_sessions r WHERE r.replaces_id = p.id) GROUP BY 1",
        [start, start + 7 * DAY, d],
    ):
        counts[r[0]] = counts.get(r[0], 0) + r[1]
    return counts


def _merged_rule(notes: list[dict[str, Any]], key: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for n in notes:
        for sport, value in ((n.get("rules") or {}).get(key) or {}).items():
            out.setdefault(sport, value)
    return out


def week_targets(db: Database, d: datetime.date, notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Weekly session targets from notes (``min_sessions_per_week``) vs what's
    done and planned.  ``required_today`` when the rest of the week can't fit
    the sessions still owed (at most one per sport per day)."""
    targets = _merged_rule(notes, "min_sessions_per_week")
    preferred = _merged_rule(notes, "preferred_weekdays")
    start = monday(d)
    days_after = 6 - d.weekday()
    out = []
    for sport, target in targets.items():
        done_days = [
            r[0]
            for r in db.fetchall(
                "SELECT CAST(start_time AS DATE) FROM activities WHERE sport_type = ? AND start_time >= ? "
                "AND start_time < ? ORDER BY 1",
                [sport, start, d + DAY],
            )
        ]
        planned_later = db.fetchone(
            "SELECT count(*) FROM planned_sessions p WHERE p.sport_type = ? AND p.date > ? AND p.date < ? "
            "AND p.activity_id IS NULL AND NOT EXISTS (SELECT 1 FROM planned_sessions r WHERE r.replaces_id = p.id)",
            [sport, d, start + 7 * DAY],
        )[0]
        owed = max(0, int(target) - len(done_days) - planned_later)
        days = [x.lower()[:3] for x in preferred.get(sport, [])]
        missed_days = [
            wd for wd in days if WEEKDAYS.index(wd) < d.weekday() and start + WEEKDAYS.index(wd) * DAY not in done_days
        ]
        out.append(
            {
                "sport": sport,
                "target_per_week": int(target),
                "done_this_week": len(done_days),
                "done_on": done_days,
                "planned_later_this_week": planned_later,
                "still_owed": owed,
                "days_left_after_target_date": days_after,
                "preferred_weekdays": days or None,
                "preferred_days_missed": missed_days or None,
                "required_today": owed > days_after,
            }
        )
    return out


def training_history(db: Database, d: datetime.date, days: int = HISTORY_DAYS) -> list[dict[str, Any]]:
    """Every session in the last *days* days (and on the target date so far)."""
    from hart.analytics.grading_features import strength_summary

    out = []
    for s in recent_sessions(db, d - days * DAY, d):
        zones = s.get("hr_zone_seconds") or {}
        total = sum(v for v in zones.values() if v) if isinstance(zones, dict) else 0
        grade = s.get("grade") or {}
        item: dict[str, Any] = {
            "date": s["date"],
            "weekday": s["date"].strftime("%a"),
            "sport": s["sport_type"],
            "indoor": s["indoor"],
            "name": s["name"],
            "minutes": round((s["duration_s"] or 0) / 60),
            "load": s.get("load"),
            "avg_hr": s.get("avg_hr"),
            "hr_zones_pct": {z: round(100 * v / total) for z, v in sorted(zones.items()) if v} if total else None,
            "grade": grade.get("letter"),
            "my_rpe": s.get("rpe"),
            "my_feel": s.get("feel"),
            "note": s.get("note"),
        }
        if s["sport_type"] == "strength":
            summary = strength_summary(db, s) or {}
            item["exercises"] = [
                {"exercise": e["exercise"], "sets": e["sets"], "top_kg": e["top_kg"], "top_reps": e["top_reps"]}
                for e in summary.get("exercises", [])
            ]
        out.append({k: v for k, v in item.items() if v is not None})
    return sorted(out, key=lambda x: x["date"])


def weekly_totals(db: Database, d: datetime.date, weeks: int = WEEKLY_TOTALS_WEEKS) -> list[dict[str, Any]]:
    start = monday(d) - weeks * 7 * DAY
    by_week: dict[datetime.date, dict[str, Any]] = {}
    for r in rows(
        db,
        "SELECT CAST(date_trunc('week', a.start_time) AS DATE) AS week, a.sport_type, count(*) AS sessions, "
        "round(sum(coalesce(a.moving_seconds, a.elapsed_seconds)) / 60) AS minutes, round(sum(m.tss)) AS load "
        "FROM activities a LEFT JOIN activity_metrics m USING (activity_id) "
        "WHERE a.start_time >= ? AND a.start_time < ? GROUP BY 1, 2 ORDER BY 1",
        [start, monday(d)],
    ):
        w = by_week.setdefault(r["week"], {"week_of": r["week"], "by_sport": {}})
        w["by_sport"][r["sport_type"]] = {"sessions": r["sessions"], "minutes": r["minutes"], "load": r["load"]}
    return list(by_week.values())


def _digest(value: Any) -> str:
    return hashlib.sha1(json.dumps(value, default=str, sort_keys=True).encode()).hexdigest()[:12]


FINGERPRINT_SECTIONS = ("readiness", "coach_plan", "training_history", "week_targets", "athlete_notes", "limits")


def previous_for(db: Database, d: datetime.date, bundle: dict[str, Any]) -> dict[str, Any] | None:
    prev = latest(db, d, ok_only=True)
    if prev is None:
        return None
    old = (prev.get("context") or {}).get("fingerprint") or {}
    changed = [k for k in FINGERPRINT_SECTIONS if old.get(k) != bundle["fingerprint"].get(k)] if old else None
    return {
        "previous_suggestion": {
            "version": prev["version"],
            "made_at": prev["created_at"],
            "recommendation": prev["recommendation"],
            "summary": prev["summary"],
            "sessions": [
                {k: x.get(k) for k in ("sport_type", "title", "duration_min", "intensity")}
                for x in prev.get("sessions") or []
            ],
        },
        "since_previous": {"changed": changed} if changed is not None else {"changed": "unknown (older version)"},
    }


PLAN_SOURCES = {"coach_import": "coach", "manual": "athlete", "suggestion_accepted": "accepted suggestion"}


def standing_plan_view(standing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "sport_type": r["sport_type"],
            "title": r["title"],
            "duration_min": r["duration_min"],
            "intensity": r["intensity"],
            "from": PLAN_SOURCES.get(r["source"], r["source"]),
            "done": bool(r.get("activity_id")),
        }
        for r in standing
    ]


def _same_session(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (
        a.get("sport_type") == b.get("sport_type")
        and (a.get("duration_min") or 0) == (b.get("duration_min") or 0)
        and (a.get("intensity") or None) == (b.get("intensity") or None)
    )


def already_planned(db: Database, s: dict[str, Any]) -> bool:
    """Every suggested session is already in the standing plan for its day (same sport, duration, intensity)."""
    sessions = s.get("sessions") or []
    standing = [r for r in active_plan_for(db, s["for_date"]) if r["sport_type"] != "rest"]
    return bool(sessions) and all(any(_same_session(x, r) for r in standing) for x in sessions)


def coach_plan_view(db: Database, d: datetime.date) -> list[dict[str, Any]]:
    """The coach's plan for a day as given to Claude (and stored in the suggestion's context)."""
    return [
        {
            "sport_type": r["sport_type"],
            "title": r["title"],
            "duration_min": r["duration_min"],
            "intensity": r["intensity"],
            "coach_text": r["description"],
            "source": r["source"],
        }
        for r in coach_plan_for(db, d)
    ]


def stale_for_plan(db: Database, dates: list[datetime.date]) -> list[datetime.date]:
    """Dates whose latest suggestion was made for a different coach plan than
    the one stored now (e.g. the coach sent tomorrow's session after 20:00)."""
    out = []
    for d in sorted(set(dates)):
        latest_row = one(
            db, "SELECT context FROM daily_suggestions WHERE for_date = ? ORDER BY version DESC LIMIT 1", [d]
        )
        if latest_row is None or pending(db, d) or race_on(db, d):
            continue
        context = latest_row["context"]
        context = json.loads(context) if isinstance(context, str) else (context or {})
        if _digest(context.get("coach_plan") or []) != _digest(coach_plan_view(db, d)):
            out.append(d)
    return out


def race_on(db: Database, d: datetime.date) -> dict[str, Any] | None:
    race = one(db, "SELECT name, distance, priority FROM races WHERE race_date = ?", [d])
    if race:
        return race
    phase = one(
        db, "SELECT name FROM training_phases WHERE phase_type = 'race' AND start_date <= ? AND end_date >= ?", [d, d]
    )
    return {"name": phase["name"], "distance": None, "priority": None} if phase else None


def build_context(
    db: Database, config: HartSettings, for_date: datetime.date, kind: str
) -> tuple[dict[str, Any], GuardContext]:
    t = state.get_thresholds(db)
    today = local_today(config)
    readiness = readiness_on(db, for_date, t)
    basis = "target date"
    if for_date > today:
        # Tomorrow's sleep doesn't exist yet: judge by today's readiness.
        readiness = readiness_on(db, today, t)
        basis = "today (preliminary — tomorrow morning's data isn't in yet)"
    season = season_context(db, min(for_date, today), t)
    notes = active_notes(db, for_date)
    coach = coach_plan_for(db, for_date)
    # What stands for the day: coach/manual sessions not replaced, plus suggestions already accepted.
    # Suggestions work against this, so re-suggesting an accepted session can't add it a second time.
    standing = active_plan_for(db, for_date)
    targets = week_targets(db, for_date, notes)
    injury_annotation = one(
        db,
        "SELECT label FROM annotations WHERE kind = 'injury' AND start_date <= ? "
        "AND (end_date IS NULL OR end_date >= ?)",
        [for_date, for_date],
    )
    guard = GuardContext(
        date=for_date,
        readiness=readiness["level"],
        coach_sessions=[
            {
                "sport_type": r["sport_type"],
                "duration_min": r["duration_min"],
                "intensity": r["intensity"],
                "title": r["title"],
            }
            for r in standing
            if r["sport_type"] != "rest"
        ],
        rules=[(n["title"], n["rules"]) for n in notes if n.get("rules")],
        week_counts=week_counts(db, for_date),
        comeback=bool(season["phase"] and season["phase"]["phase_type"] == "comeback"),
        longest_min_14d=longest_recent(db, for_date),
        injury_active=bool(injury_annotation) or any(n["category"] == "injury" for n in notes),
        required_today={
            t["sport"]: f"{t['still_owed']} {t['sport']} session(s) still owed this week and "
            f"{t['days_left_after_target_date']} day(s) left after {for_date:%A}"
            for t in targets
            if t["required_today"]
        },
    )
    # A plan row "rest" still means the coach planned the day.
    coach_rest = any(r["sport_type"] == "rest" for r in coach)
    if coach_rest and not guard.coach_sessions:
        guard.coach_sessions = [{"sport_type": "rest", "duration_min": 0, "intensity": "recovery", "title": "Rest"}]

    week_start = monday(for_date)
    history = training_history(db, for_date)

    bundle = {
        "target_date": for_date,
        "weekday": for_date.strftime("%A"),
        "kind": kind,
        "generated_for": basis,
        "readiness": {k: readiness.get(k) for k in ("level", "reason", "inputs", "hits", "steps")} | {"basis": basis},
        "coach_plan": coach_plan_view(db, for_date),
        "standing_plan": standing_plan_view(standing),
        "season": {
            "phase": season["phase"],
            "observed_state": season["state"],
            "flags": season["flags"],
        },
        "training_history": history,
        "weekly_totals": weekly_totals(db, for_date),
        "week_targets": targets,
        "this_week": {
            "week_of": week_start,
            "planned": [
                {
                    "date": r["date"],
                    "sport_type": r["sport_type"],
                    "title": r["title"],
                    "duration_min": r["duration_min"],
                    "done": bool(r["activity_id"]),
                }
                for r in rows(
                    db,
                    "SELECT * FROM planned_sessions WHERE date >= ? AND date <= ? ORDER BY date",
                    [week_start, week_start + 6 * DAY],
                )
            ],
        },
        "done_on_target_date": [h for h in history if h["date"] == for_date],
        "athlete_notes": [
            {"category": n["category"], "title": n["title"], "body": n["body"], "rules": n.get("rules")} for n in notes
        ],
        "limits": limits(guard),
    }
    bundle["fingerprint"] = {k: _digest(bundle[k]) for k in FINGERPRINT_SECTIONS}
    bundle |= previous_for(db, for_date, bundle) or {}
    return bundle, guard


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _next_version(db: Database, for_date: datetime.date) -> int:
    return db.fetchone("SELECT coalesce(max(version), 0) + 1 FROM daily_suggestions WHERE for_date = ?", [for_date])[0]


def store(db: Database, for_date: datetime.date, fields: dict[str, Any]) -> int:
    version = _next_version(db, for_date)
    cols = ["for_date", "version", *fields]
    values = [
        for_date,
        version,
        *[json.dumps(v, default=str) if isinstance(v, dict | list) else v for v in fields.values()],
    ]
    return db.fetchone(
        f"INSERT INTO daily_suggestions ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) RETURNING id",
        values,
    )[0]


class SuggestionsPaused(Exception):
    pass


def _argued(output: SuggestionOutput) -> bool:
    """A case against the coach's session: a stance other than 'agree', with the reasons written out."""
    return output.coach_stance in ("partly", "disagree") and bool((output.coach_take or "").strip())


def _parse(outcome: RunOutcome) -> tuple[SuggestionOutput | None, str | None]:
    if outcome.status != "ok":
        return None, outcome.error
    raw = _structured_raw(outcome)
    if raw is None:
        return None, "no structured output"
    try:
        return SuggestionOutput.model_validate(raw), None
    except ValidationError as exc:
        return None, str(exc)[:800]


def generate(
    db: Database,
    for_date: datetime.date,
    kind: str,
    claude: ClaudeRunner,
    loop: asyncio.AbstractEventLoop,
    config: HartSettings,
    *,
    trigger: str = "manual",
) -> dict[str, Any]:
    """Generate and store one suggestion (runs in a job thread)."""
    race = race_on(db, for_date)
    if race:
        return {"status": "skipped", "reason": f"race day — {race['name']}"}
    paused = claude_paused(db)
    if paused:
        raise SuggestionsPaused(f"Claude paused until {paused}")

    bundle, guard = build_context(db, config, for_date, kind)
    bundle["trigger"] = trigger
    facts = json.dumps(bundle, default=str, indent=1)
    prompt = (
        f"Suggest the training for {for_date.isoformat()} ({for_date.strftime('%A')}). Facts:\n```json\n{facts}\n```"
    )
    spec = RunSpec(
        purpose="suggest",
        prompt=prompt,
        model=settings.get(db, "model_suggest"),
        system_prompt=system_prompt(athlete_profile(db)),
        prompt_version=SUGGEST_PROMPT_VERSION,
        policy=read_only_policy(),
        max_turns=12,
        timeout_s=300,
        background=True,
        output_schema=SuggestionOutput.model_json_schema(),
    )

    def run(s: RunSpec) -> RunOutcome:
        return asyncio.run_coroutine_threadsafe(claude.run(s), loop).result(timeout=s.timeout_s + 60)

    base = {"kind": kind, "readiness": guard.readiness, "readiness_detail": bundle["readiness"], "context": bundle}
    outcome = run(spec)
    if outcome.status in ("usage_limited", "auth_failed"):
        pause_claude(db, outcome)
        raise SuggestionsPaused(outcome.error or outcome.status)

    output, error = _parse(outcome)
    violations: list[str] = []
    if output is not None:
        violations = check(
            output.recommendation, [s.model_dump() for s in output.sessions], guard, argued=_argued(output)
        )
    if (output is None and outcome.status == "ok") or violations:
        problem = f"it broke these limits: {'; '.join(violations)}" if violations else f"it failed validation: {error}"
        repair = RunSpec(
            **{
                **spec.__dict__,
                "resume": outcome.session_id,
                "prompt": f"Your previous answer was rejected because {problem}. Return a corrected suggestion.",
            }
        )
        outcome = run(repair)
        if outcome.status in ("usage_limited", "auth_failed"):
            pause_claude(db, outcome)
            raise SuggestionsPaused(outcome.error or outcome.status)
        output, error = _parse(outcome)
        violations = (
            check(output.recommendation, [s.model_dump() for s in output.sessions], guard, argued=_argued(output))
            if output
            else []
        )

    if output is None or violations:
        reason = ("guardrails: " + "; ".join(violations)) if violations else (error or outcome.error or "no output")
        suggestion_id = store(
            db, for_date, {**base, "status": "failed", "error": reason[:1000], "claude_run_id": outcome.run_id}
        )
        return {"status": "failed", "suggestion_id": suggestion_id, "error": reason}

    bundle["week_review"] = output.week_review
    if guard.coach_sessions and output.coach_stance:
        bundle["coach_take"] = {"stance": output.coach_stance, "text": output.coach_take}
    bundle["changed_from_previous"] = output.changed_from_previous
    citations = verify_citations(output.citations, [facts, *outcome.tool_outputs])
    sessions = [s.model_dump() for s in output.sessions]
    for s in sessions:
        if s["sport_type"] != "bike":
            s["garmin_text"] = None
    suggestion_id = store(
        db,
        for_date,
        {
            **base,
            "recommendation": output.recommendation,
            "sessions": sessions,
            "cautions": output.cautions,
            "summary": output.summary,
            "citations": citations,
            "status": "ok",
            "claude_run_id": outcome.run_id,
        },
    )
    return {"status": "ok", "suggestion_id": suggestion_id, "recommendation": output.recommendation}


# ---------------------------------------------------------------------------
# Reads and accept
# ---------------------------------------------------------------------------

COLUMNS = (
    "id, for_date, version, kind, readiness, readiness_detail, recommendation, sessions, cautions, summary, "
    "citations, status, error, context, claude_run_id, created_at"
)


def _decode(s: dict[str, Any]) -> dict[str, Any]:
    for key in ("readiness_detail", "sessions", "cautions", "citations", "context"):
        if isinstance(s.get(key), str):
            s[key] = json.loads(s[key])
    return s


def get(db: Database, suggestion_id: int) -> dict[str, Any] | None:
    found = one(db, f"SELECT {COLUMNS} FROM daily_suggestions WHERE id = ?", [suggestion_id])
    return _decode(found) if found else None


def latest(db: Database, for_date: datetime.date, *, ok_only: bool = False) -> dict[str, Any] | None:
    where = "AND status = 'ok'" if ok_only else ""
    found = one(
        db,
        f"SELECT {COLUMNS} FROM daily_suggestions WHERE for_date = ? {where} ORDER BY version DESC LIMIT 1",
        [for_date],
    )
    if not found:
        return None
    s = _decode(found)
    accepted = rows(
        db,
        "SELECT p.id, p.sport_type, p.garmin_status, p.garmin_error, "
        "EXISTS (SELECT 1 FROM jobs j WHERE j.dedupe_key = 'garmin_workout:' || p.id "
        "        AND j.status IN ('queued', 'running')) AS garmin_pending "
        "FROM planned_sessions p WHERE p.suggestion_id = ? ORDER BY p.id",
        [s["id"]],
    )
    s["accepted"] = [r["id"] for r in accepted]
    s["accepted_rows"] = accepted
    return s


def for_display(db: Database, for_date: datetime.date) -> dict[str, Any] | None:
    """The latest good suggestion, or the latest failure if none succeeded."""
    good = latest(db, for_date, ok_only=True)
    last = latest(db, for_date)
    if good is None:
        return last
    if last and last["id"] != good["id"] and last["status"] == "failed":
        good["newer_failed"] = {"version": last["version"], "error": last["error"]}
    good["already_planned"] = good["status"] == "ok" and already_planned(db, good)
    return good


def pending(db: Database, for_date: datetime.date) -> bool:
    return (
        db.fetchone(
            "SELECT 1 FROM jobs WHERE dedupe_key = ? AND status IN ('queued', 'running')", [f"suggest:{for_date}"]
        )
        is not None
    )


def accept(db: Database, suggestion_id: int) -> list[int]:
    s = get(db, suggestion_id)
    if s is None:
        raise ValueError("suggestion not found")
    if s["status"] != "ok" or s["recommendation"] not in ACCEPTABLE:
        raise ValueError("only free-choice, modified or replacement suggestions can be accepted")
    if db.fetchone("SELECT 1 FROM planned_sessions WHERE suggestion_id = ?", [suggestion_id]):
        raise ValueError("already accepted")
    if already_planned(db, s):
        raise ValueError("these sessions are already in your plan")
    # A modified or replacing suggestion takes the place of what stands (coach sessions or an earlier
    # accepted suggestion) — never next to it.
    coach = (
        [r for r in active_plan_for(db, s["for_date"]) if r["sport_type"] != "rest" and not r.get("activity_id")]
        if s["recommendation"] in ("modify", "replace")
        else []
    )
    ids = []
    for i, sess in enumerate(s["sessions"]):
        description = "\n\n".join(x for x in (sess.get("structure"), sess.get("rationale")) if x)
        new_id = create_row(
            db,
            PlanRowIn(
                date=s["for_date"],
                sport_type=sess["sport_type"],
                title=sess["title"][:120],
                description=description or None,
                duration_min=sess["duration_min"],
                intensity=sess["intensity"],
            ),
            "suggestion_accepted",
            replaces_id=coach[i]["id"] if i < len(coach) else None,
            suggestion_id=suggestion_id,
        )
        if sess.get("garmin_text"):
            db.execute("UPDATE planned_sessions SET garmin_text = ? WHERE id = ?", [sess["garmin_text"], new_id])
        ids.append(new_id)
    return ids


# ---------------------------------------------------------------------------
# Schedule helpers and dashboard card
# ---------------------------------------------------------------------------


def local_now(config: HartSettings) -> datetime.datetime:
    return datetime.datetime.now(ZoneInfo(config.server.tz))


def finals(db: Database, d: datetime.date) -> list[dict[str, Any]]:
    return rows(
        db,
        "SELECT id, readiness, status, context->>'trigger' AS trigger FROM daily_suggestions "
        "WHERE for_date = ? AND kind = 'final' ORDER BY version",
        [d],
    )


def should_generate_final(db: Database, d: datetime.date) -> bool:
    """After last night's sleep arrived: no final yet, or only a fallback one
    made without sleep (regenerated once)."""
    made = finals(db, d)
    if not made:
        return True
    return len(made) == 1 and made[0]["readiness"] == "unknown" and made[0]["trigger"] == "fallback"


def preliminary_count(db: Database, d: datetime.date) -> int:
    return db.fetchone("SELECT count(*) FROM daily_suggestions WHERE for_date = ? AND kind = 'preliminary'", [d])[0]


def today_done(db: Database, today: datetime.date) -> dict[str, Any] | None:
    """Today's training is done: every planned session (coach or accepted suggestion) has its activity, or —
    with nothing planned — a training session was logged. Returns the session to show, or None."""
    from hart.server.plan import active_plan_for

    plan = [r for r in active_plan_for(db, today) if r["sport_type"] != "rest"]
    done = recent_sessions(db, today, today)
    done_training = [s for s in done if s["sport_type"] in TRAINING_SPORTS]
    if not ((plan and all(r["activity_id"] for r in plan)) or (not plan and done_training)):
        return None
    key = plan[0]["activity_id"] if plan else done_training[0]["activity_id"]
    session = next((s for s in done if s["activity_id"] == key), done_training[0] if done_training else None)
    return {"activity_id": key, "name": session["name"] if session else None, "grade": (session or {}).get("grade")}


def dashboard_card(db: Database, config: HartSettings) -> dict[str, Any]:
    now = local_now(config)
    today = now.date()
    tomorrow = today + DAY
    evening = now.time() >= settings.get_time(db, "preliminary_time")
    target = tomorrow if evening else today
    card: dict[str, Any] = {"date": target, "is_tomorrow": evening, "evening": evening}

    race = race_on(db, target)
    if race:
        return {**card, "race": race}

    card["coach"] = coach_plan_for(db, target)
    if not evening:
        done = today_done(db, today)
        if done:
            # Today is done: tomorrow's suggestion (made as soon as the session synced) takes the card.
            card["done"] = done
            card["tomorrow"] = {
                "date": tomorrow,
                "is_tomorrow": True,
                "race": race_on(db, tomorrow),
                "coach": coach_plan_for(db, tomorrow),
                "suggestion": for_display(db, tomorrow),
                "pending": pending(db, tomorrow),
                "claude_paused": claude_paused(db),
            }
    card["suggestion"] = for_display(db, target)
    card["pending"] = pending(db, target)
    card["claude_paused"] = claude_paused(db)
    return card
