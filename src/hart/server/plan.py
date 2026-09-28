"""Training plan: planned sessions, paste import of a coach's plan, activity matching
and Garmin workout text.

There is deliberately no platform integration (coaching platforms need paid API
access, and plans often arrive by email or message anyway): the athlete copies the week and pastes it; Claude turns the text into rows,
the athlete checks a preview, and only then are rows saved.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from hart.server.claude.policy import ToolPolicy
from hart.server.claude.runner import ClaudeRunner, RunSpec
from hart.server.data import DAY, monday, rows
from hart.server.grading import _structured_raw
from hart.storage.database import Database

PLAN_SPORTS = ("swim", "bike", "run", "strength", "other", "rest")
INTENSITIES = ("recovery", "endurance", "tempo", "threshold", "vo2", "strength", "mixed")
SOURCES = ("manual", "coach_import", "suggestion_accepted")
PARSE_PROMPT_VERSION = "parse_plan@2"
GARMIN_PROMPT_VERSION = "garmin_text@1"
MAX_PASTE_CHARS = 20000
SKILL_PATH = Path(__file__).resolve().parents[3] / ".claude" / "skills" / "garmin-workout-formatter" / "SKILL.md"

PlanSport = Literal["swim", "bike", "run", "strength", "other", "rest"]
Intensity = Literal["recovery", "endurance", "tempo", "threshold", "vo2", "strength", "mixed"]


class PlanError(Exception):
    pass


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

PLAN_COLUMNS = (
    "p.id, p.date, p.sport_type, p.title, p.description, p.duration_min, p.intensity, p.source, "
    "p.replaces_id, p.activity_id, p.match_method, p.suggestion_id, p.garmin_text, p.created_at, "
    "p.garmin_status, p.garmin_error, p.garmin_workout_id, p.garmin_sent_at"
)


def plan_rows(db: Database, start: datetime.date, end: datetime.date) -> list[dict[str, Any]]:
    """Plan rows in a date range with their matched activity and replacement state."""
    found = rows(
        db,
        f"SELECT {PLAN_COLUMNS}, a.name AS activity_name, a.sport_type AS activity_sport, "
        "coalesce(a.moving_seconds, a.elapsed_seconds) AS activity_seconds, "
        "(SELECT r.id FROM planned_sessions r WHERE r.replaces_id = p.id LIMIT 1) AS replaced_by "
        "FROM planned_sessions p LEFT JOIN activities a ON a.activity_id = p.activity_id "
        "WHERE p.date >= ? AND p.date <= ? ORDER BY p.date, p.replaces_id NULLS FIRST, p.id",
        [start, end],
    )
    if found:
        from hart.server.grading import grade_chips

        chips = grade_chips(db, [r["activity_id"] for r in found if r["activity_id"]])
        for r in found:
            r["grade"] = chips.get(r["activity_id"]) if r["activity_id"] else None
    return found


def active_plan_for(db: Database, d: datetime.date) -> list[dict[str, Any]]:
    """The plan that stands for a day: coach/manual rows not replaced by an
    accepted suggestion, plus accepted suggestions."""
    return [r for r in plan_rows(db, d, d) if r["replaced_by"] is None]


def coach_plan_for(db: Database, d: datetime.date) -> list[dict[str, Any]]:
    """What the coach (or I, manually) planned for a day — never accepted suggestions."""
    return [r for r in plan_rows(db, d, d) if r["source"] != "suggestion_accepted"]


def calendar(db: Database, today: datetime.date, weeks: int = 3) -> list[dict[str, Any]]:
    start = monday(today)
    end = start + (7 * weeks - 1) * DAY
    by_day: dict[datetime.date, list[dict[str, Any]]] = {}
    for r in plan_rows(db, start, end):
        by_day.setdefault(r["date"], []).append(r)
    done = rows(
        db,
        "SELECT activity_id, sport_type, sub_type, name, CAST(start_time AS DATE) AS date, "
        "coalesce(moving_seconds, elapsed_seconds) AS seconds FROM activities "
        "WHERE start_time >= ? AND start_time < ? ORDER BY start_time",
        [start, end + DAY],
    )
    matched = {r["activity_id"] for day in by_day.values() for r in day if r["activity_id"]}
    out = []
    for w in range(weeks):
        days = []
        for i in range(7):
            d = start + (7 * w + i) * DAY
            days.append(
                {
                    "date": d,
                    "is_today": d == today,
                    "past": d < today,
                    "planned": by_day.get(d, []),
                    "unplanned": [a for a in done if a["date"] == d and a["activity_id"] not in matched],
                    "activities": [a for a in done if a["date"] == d],
                }
            )
        out.append({"start": start + 7 * w * DAY, "days": days})
    return out


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class PlanRowIn(BaseModel):
    date: datetime.date
    sport_type: PlanSport
    title: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=5000)
    duration_min: int | None = Field(default=None, ge=0, le=900)
    intensity: Intensity | None = None


def create_row(
    db: Database,
    row: PlanRowIn,
    source: str = "manual",
    *,
    replaces_id: int | None = None,
    suggestion_id: int | None = None,
) -> int:
    new_id = db.fetchone(
        "INSERT INTO planned_sessions (date, sport_type, title, description, duration_min, intensity, source, "
        "replaces_id, suggestion_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        [
            row.date,
            row.sport_type,
            row.title.strip(),
            (row.description or "").strip() or None,
            row.duration_min,
            row.intensity,
            source,
            replaces_id,
            suggestion_id,
        ],
    )[0]
    auto_match(db, [row.date])
    return new_id


def update_row(db: Database, row_id: int, row: PlanRowIn) -> None:
    old = db.fetchone("SELECT date, description FROM planned_sessions WHERE id = ?", [row_id])
    if old is None:
        raise PlanError("planned session not found")
    description = (row.description or "").strip() or None
    before = db.fetchone(
        "SELECT date, sport_type, title, description, duration_min, intensity FROM planned_sessions WHERE id = ?",
        [row_id],
    )
    db.execute(
        "UPDATE planned_sessions SET date = ?, sport_type = ?, title = ?, description = ?, duration_min = ?, "
        "intensity = ?, garmin_text = CASE WHEN ? THEN garmin_text ELSE NULL END, "
        "activity_id = CASE WHEN match_method = 'auto' THEN NULL ELSE activity_id END, "
        "match_method = CASE WHEN match_method = 'auto' THEN NULL ELSE match_method END WHERE id = ?",
        [
            row.date,
            row.sport_type,
            row.title.strip(),
            description,
            row.duration_min,
            row.intensity,
            description == old[1],
            row_id,
        ],
    )
    after = db.fetchone(
        "SELECT date, sport_type, title, description, duration_min, intensity FROM planned_sessions WHERE id = ?",
        [row_id],
    )
    if before != after:
        # The workout on Garmin no longer matches the plan: offer a re-send.
        db.execute(
            "UPDATE planned_sessions SET garmin_status = 'outdated' WHERE id = ? AND garmin_workout_id IS NOT NULL",
            [row_id],
        )
    auto_match(db, sorted({old[0], row.date}))


def delete_row(db: Database, row_id: int) -> str | None:
    """Delete a plan row; returns its Garmin workout id (to delete there too)."""
    found = db.fetchone("SELECT garmin_workout_id FROM planned_sessions WHERE id = ?", [row_id])
    if found is None:
        raise PlanError("planned session not found")
    # Deleting an accepted suggestion restores the coach row it replaced;
    # deleting a coach row drops the link from anything that replaced it.
    db.execute("UPDATE planned_sessions SET replaces_id = NULL WHERE replaces_id = ?", [row_id])
    db.execute("DELETE FROM planned_sessions WHERE id = ?", [row_id])
    return found[0]


def link_activity(db: Database, row_id: int, activity_id: str | None) -> None:
    found = db.fetchone("SELECT date FROM planned_sessions WHERE id = ?", [row_id])
    if found is None:
        raise PlanError("planned session not found")
    if activity_id is None:
        # "Not done": manual with no activity, so auto-matching leaves it alone.
        db.execute("UPDATE planned_sessions SET activity_id = NULL, match_method = 'manual' WHERE id = ?", [row_id])
        return
    if db.fetchone("SELECT 1 FROM activities WHERE activity_id = ?", [activity_id]) is None:
        raise PlanError("activity not found")
    db.execute(
        "UPDATE planned_sessions SET activity_id = NULL, match_method = NULL "
        "WHERE activity_id = ? AND id <> ? AND match_method = 'auto'",
        [activity_id, row_id],
    )
    db.execute(
        "UPDATE planned_sessions SET activity_id = ?, match_method = 'manual' WHERE id = ?", [activity_id, row_id]
    )


def auto_match(db: Database, dates: list[datetime.date]) -> int:
    """Link completed activities to planned sessions on the same date and sport
    (indoor and outdoor count as the same sport); the closest duration wins.
    Manual links are never touched.  Returns the number of new links."""
    linked = 0
    for d in sorted(set(dates)):
        planned = rows(
            db,
            "SELECT p.id, p.sport_type, p.duration_min, "
            "EXISTS (SELECT 1 FROM planned_sessions r WHERE r.replaces_id = p.id) AS replaced "
            "FROM planned_sessions p WHERE p.date = ? AND p.activity_id IS NULL "
            "AND p.match_method IS NULL AND p.sport_type <> 'rest' ORDER BY replaced, p.id",
            [d],
        )
        if not planned:
            continue
        taken = {
            r[0]
            for r in db.fetchall(
                "SELECT activity_id FROM planned_sessions WHERE activity_id IS NOT NULL AND date = ?", [d]
            )
        }
        acts = [
            a
            for a in rows(
                db,
                "SELECT activity_id, sport_type, coalesce(moving_seconds, elapsed_seconds) AS seconds "
                "FROM activities WHERE CAST(start_time AS DATE) = ?",
                [d],
            )
            if a["activity_id"] not in taken
        ]
        for p in planned:
            candidates = [a for a in acts if a["sport_type"] == p["sport_type"]]
            if not candidates:
                continue
            target = (p["duration_min"] or 0) * 60
            best = min(candidates, key=lambda a: abs((a["seconds"] or 0) - target) if target else 0)
            db.execute(
                "UPDATE planned_sessions SET activity_id = ?, match_method = 'auto' WHERE id = ?",
                [best["activity_id"], p["id"]],
            )
            acts.remove(best)
            linked += 1
    return linked


# ---------------------------------------------------------------------------
# Pasted plan text → rows (Claude, the parsing model)
# ---------------------------------------------------------------------------


class ParsedSession(BaseModel):
    date: datetime.date
    sport_type: PlanSport
    title: str = Field(max_length=120)
    duration_min: int | None = Field(default=None, ge=0, le=900)
    intensity: Intensity | None = None
    description: str = Field(description="The coach's text for this workout, copied verbatim from the paste")


class ParsedPlan(BaseModel):
    sessions: list[ParsedSession] = Field(max_length=40)
    warnings: list[str] = Field(default_factory=list, max_length=10)


_PARSE_SYSTEM = """\
You convert a training plan pasted from a coaching app, an email or a message, or typed by hand \
into structured rows. Copied text contains artefacts: day headers \
("Monday, September 28", "Pon 28.09"), sport labels (Bike, Run, Swim, Strength, Day Off/Rest), \
planned duration lines ("1:20:00", "Planned: 45m"), TSS / IF / distance lines, and workout-builder \
step lists. Coach notes may be shorthand in any language, e.g. Polish "30'- progresja do 170W, kadencja powyżej 80" \
(30' = 30 minutes, W = watts, ROZJAZD = cool-down, rozgrzewka = warm-up, basen = pool, siła = strength).

Rules:
- One row per workout. Resolve every date using the reference date given; dates without a year are \
the nearest matching future-or-current date.
- sport_type: swim | bike | run | strength | other | rest (rest only for an explicit day off).
- duration_min: the planned duration in minutes if stated, else the sum of the steps if unambiguous, else null.
- intensity: recovery | endurance | tempo | threshold | vo2 | strength | mixed, inferred from the text; \
null when unclear.
- title: short English title (e.g. "Endurance ride with progression").
- description: the coach's full text for that workout, copied verbatim (keep the original language, keep line \
breaks). Include TSS/IF lines verbatim here but never interpret them.
- Anything you couldn't place goes into warnings. Never invent workouts.
"""


async def parse_paste(
    claude: ClaudeRunner, model: str, text: str, today: datetime.date, default_date: datetime.date | None = None
) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise PlanError("nothing to parse")
    if len(text) > MAX_PASTE_CHARS:
        raise PlanError(f"paste is too long (max {MAX_PASTE_CHARS} characters) — paste one week at a time")
    spec = RunSpec(
        purpose="parse_plan",
        prompt=(
            f"Reference date: {today.isoformat()} ({today.strftime('%A')}).\n"
            + (
                f"Workouts the text doesn't date belong to {default_date.isoformat()} "
                f"({default_date.strftime('%A')}).\n"
                if default_date
                else ""
            )
            + f"\nPasted text:\n<<<\n{text}\n>>>"
        ),
        model=model,
        system_prompt=_PARSE_SYSTEM,
        prompt_version=PARSE_PROMPT_VERSION,
        policy=ToolPolicy(allowed_mcp=frozenset(), web=False),
        max_turns=3,
        timeout_s=90,
        output_schema=ParsedPlan.model_json_schema(),
    )
    outcome = await claude.run(spec)
    if outcome.status != "ok":
        raise PlanError(outcome.error or f"Claude run {outcome.status}")
    try:
        parsed = ParsedPlan.model_validate(_structured_raw(outcome))
    except ValidationError as exc:
        raise PlanError(f"Couldn't read the plan: {str(exc)[:300]}") from exc
    return {
        "sessions": [s.model_dump(mode="json") for s in parsed.sessions],
        "warnings": parsed.warnings,
        "claude_run_id": outcome.run_id,
    }


def import_rows(db: Database, sessions: list[PlanRowIn], replace_existing: bool = True) -> dict[str, Any]:
    """Save confirmed coach rows.  With *replace_existing*, earlier coach
    imports on the same dates are replaced (a re-pasted week wins), unless
    they were already linked to an activity."""
    dates = sorted({s.date for s in sessions})
    removed = 0
    garmin_removed: list[str] = []
    if replace_existing and dates:
        placeholders = ", ".join("?" for _ in dates)
        old = [
            r[0]
            for r in db.fetchall(
                f"SELECT id FROM planned_sessions WHERE source = 'coach_import' AND date IN ({placeholders}) "
                "AND activity_id IS NULL",
                dates,
            )
        ]
        for row_id in old:
            workout_id = delete_row(db, row_id)
            if workout_id:
                garmin_removed.append(workout_id)
        removed = len(old)
    ids = [
        db.fetchone(
            "INSERT INTO planned_sessions (date, sport_type, title, description, duration_min, intensity, source) "
            "VALUES (?, ?, ?, ?, ?, ?, 'coach_import') RETURNING id",
            [s.date, s.sport_type, s.title.strip(), (s.description or "").strip() or None, s.duration_min, s.intensity],
        )[0]
        for s in sessions
    ]
    auto_match(db, dates)
    return {"created": len(ids), "replaced": removed, "ids": ids, "garmin_workouts_removed": garmin_removed}


# ---------------------------------------------------------------------------
# Garmin workout text (the garmin-workout-formatter skill)
# ---------------------------------------------------------------------------


def garmin_rules() -> str:
    text = SKILL_PATH.read_text(encoding="utf-8") if SKILL_PATH.is_file() else ""
    return re.sub(r"^---.*?---\s*", "", text, flags=re.S)  # drop the skill's front matter


def extract_text_block(text: str) -> str | None:
    match = re.search(r"```(?:text)?\s*\n(.*?)```", text or "", re.S)
    body = (match.group(1) if match else text or "").strip()
    return body or None


async def garmin_text(claude: ClaudeRunner, model: str, coach_text: str) -> str:
    rules = garmin_rules()
    if not rules:
        raise PlanError("Garmin formatter rules not found")
    spec = RunSpec(
        purpose="garmin_text",
        prompt=f"Convert this workout:\n\n{coach_text.strip()}",
        model=model,
        system_prompt=rules,
        prompt_version=GARMIN_PROMPT_VERSION,
        policy=ToolPolicy(allowed_mcp=frozenset(), web=False),
        max_turns=2,
        timeout_s=120,
    )
    outcome = await claude.run(spec)
    if outcome.status != "ok":
        raise PlanError(outcome.error or f"Claude run {outcome.status}")
    result = extract_text_block(outcome.text)
    if not result:
        raise PlanError("Claude returned no workout text")
    return result
