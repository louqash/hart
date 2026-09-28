"""Send planned sessions to Garmin Connect as structured workouts.

Claude (the grading model, no tools) turns a session's text — the coach's
description or an accepted suggestion's structure — into simple steps:
blocks of warm-up / interval / recovery / cool-down steps with a duration and
at most one main target (a power range, a heart-rate zone) plus an optional
cadence range.  Code validates the steps, builds the Garmin workout JSON,
uploads it and schedules it on the plan date, so it appears on the watch and
the Edge.  Re-sending replaces the previous workout; deleting the plan row
deletes it from Garmin.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from hart.config import HartSettings
from hart.server import settings
from hart.server.claude.policy import ToolPolicy
from hart.server.claude.runner import ClaudeRunner, RunSpec
from hart.server.data import one
from hart.storage.database import Database

PROMPT_VERSION = "garmin_steps@1"
SPORTS = {"run": ("running", 1), "bike": ("cycling", 2)}
DURATION_TOLERANCE = 0.25


class GarminError(Exception):
    pass


# ---------------------------------------------------------------------------
# Steps (what Claude returns)
# ---------------------------------------------------------------------------


class Step(BaseModel):
    kind: Literal["warmup", "interval", "recovery", "cooldown", "rest"]
    minutes: float = Field(gt=0, le=300)
    target: Literal["none", "power", "hr_zone"] = "none"
    power_low: int | None = Field(default=None, ge=30, le=1500)
    power_high: int | None = Field(default=None, ge=30, le=1500)
    hr_zone: int | None = Field(default=None, ge=1, le=5)
    cadence_low: int | None = Field(default=None, ge=40, le=220)
    cadence_high: int | None = Field(default=None, ge=40, le=220)
    note: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def _targets(self) -> Step:
        if self.target == "power" and not (self.power_low and self.power_high and self.power_low <= self.power_high):
            raise ValueError("a power target needs power_low <= power_high (watts)")
        if self.target == "hr_zone" and self.hr_zone is None:
            raise ValueError("an hr_zone target needs hr_zone (1-5)")
        if (self.cadence_low is None) != (self.cadence_high is None) or (
                self.cadence_low and self.cadence_low > self.cadence_high):
            raise ValueError("cadence needs both cadence_low <= cadence_high")
        return self


class Block(BaseModel):
    repeat: int = Field(default=1, ge=1, le=40)
    steps: list[Step] = Field(min_length=1, max_length=12)


class WorkoutSteps(BaseModel):
    name: str = Field(max_length=60, description="Short workout name for the watch")
    blocks: list[Block] = Field(min_length=1, max_length=30)

    def minutes(self) -> float:
        return sum(b.repeat * sum(s.minutes for s in b.steps) for b in self.blocks)


_SYSTEM = """\
You convert one planned training session into structured workout steps for a Garmin watch / bike \
computer. The session text may be coach shorthand in any language (e.g. Polish "15'- progresja do 170W, kadencja >80", \
"3x(5'-200W + 2'-150W)", "ROZJAZD" = cool-down) or English structure lines.

Rules:
- Follow the text exactly; never add work that isn't there. If it only says e.g. "30' easy Z2", \
make one interval step of 30 min with hr_zone 2 (runs) — or split into warm-up / main / cool-down \
only if the text does.
- Loops become blocks with `repeat`; flatten nested loops into the block's steps.
- Bike power: a single number becomes a ±5 W range (250W → 245–255); keep given ranges. A ramp \
("progresja do 170W") becomes several warm-up steps climbing ~10 W per 2–3 min from ~100 W, ending \
exactly at the target, summing to the stated time.
- Cadence ">80" becomes 80–110; a closed range is kept; no cadence if none is given.
- Runs: use hr_zone when the text gives a zone (Z1/Z2 → 1/2); otherwise target "none".
- Durations in minutes (fractions allowed, e.g. 0.5 = 30 s). The total should match the planned duration.
- name: short, e.g. "Z2 run 30'" or "3x5' @200W".
"""


async def convert(claude: ClaudeRunner, model: str, row: dict[str, Any]) -> WorkoutSteps:
    from hart.server.grading import _structured_raw

    text = "\n".join(x for x in (
        f"Sport: {row['sport_type']}",
        f"Title: {row['title']}",
        f"Planned duration: {row['duration_min']} min" if row.get("duration_min") else None,
        f"Intensity: {row['intensity']}" if row.get("intensity") else None,
        f"Text:\n{row['description']}" if row.get("description") else None,
        f"Garmin text (already expanded):\n{row['garmin_text']}" if row.get("garmin_text") else None,
    ) if x)
    spec = RunSpec(
        purpose="garmin_steps", prompt=text, model=model, system_prompt=_SYSTEM,
        prompt_version=PROMPT_VERSION, policy=ToolPolicy(allowed_mcp=frozenset(), web=False), max_turns=3,
        timeout_s=120, background=True, output_schema=WorkoutSteps.model_json_schema(),
    )
    outcome = await claude.run(spec)
    if outcome.status != "ok":
        raise GarminError(outcome.error or f"Claude run {outcome.status}")
    try:
        steps = WorkoutSteps.model_validate(_structured_raw(outcome))
    except ValidationError as exc:
        raise GarminError(f"Couldn't turn the session into steps: {str(exc)[:300]}") from exc
    planned = row.get("duration_min")
    if planned and abs(steps.minutes() - planned) > planned * DURATION_TOLERANCE:
        raise GarminError(f"The steps add up to {steps.minutes():.0f} min but the session is {planned} min — "
                          "check the session text")
    return steps


# ---------------------------------------------------------------------------
# Garmin workout JSON (code)
# ---------------------------------------------------------------------------

_STEP_TYPES = {"warmup": (1, 1), "cooldown": (2, 2), "interval": (3, 3), "recovery": (4, 4), "rest": (5, 5)}
_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time", "displayOrder": 2, "displayable": True}
_NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
_POWER = {"workoutTargetTypeId": 2, "workoutTargetTypeKey": "power.zone", "displayOrder": 2}
_CADENCE = {"workoutTargetTypeId": 3, "workoutTargetTypeKey": "cadence", "displayOrder": 3}
_HR = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4}


def _executable(step: Step, order: int, child: int | None) -> dict[str, Any]:
    type_id, display = _STEP_TYPES[step.kind]
    out: dict[str, Any] = {
        "type": "ExecutableStepDTO", "stepOrder": order,
        "stepType": {"stepTypeId": type_id, "stepTypeKey": step.kind, "displayOrder": display},
        "endCondition": _TIME, "endConditionValue": round(step.minutes * 60),
        "targetType": _NO_TARGET,
    }
    if child is not None:
        out["childStepId"] = child
    if step.note:
        out["description"] = step.note
    if step.target == "power":
        out |= {"targetType": _POWER, "targetValueOne": step.power_low, "targetValueTwo": step.power_high}
    elif step.target == "hr_zone":
        out |= {"targetType": _HR, "zoneNumber": step.hr_zone}
    if step.cadence_low is not None:
        cadence = {"targetValueOne": step.cadence_low, "targetValueTwo": step.cadence_high}
        if out["targetType"] is _NO_TARGET:
            out |= {"targetType": _CADENCE, **cadence}
        else:
            out |= {"secondaryTargetType": _CADENCE, "secondaryTargetValueOne": step.cadence_low,
                    "secondaryTargetValueTwo": step.cadence_high}
    return out


def build_workout(sport: str, steps: WorkoutSteps, description: str | None = None) -> dict[str, Any]:
    if sport not in SPORTS:
        raise GarminError(f"{sport} sessions can't be sent to Garmin yet (runs and rides only)")
    key, sport_id = SPORTS[sport]
    sport_type = {"sportTypeId": sport_id, "sportTypeKey": key, "displayOrder": sport_id}
    order, group, out_steps = 0, 0, []
    for block in steps.blocks:
        if block.repeat == 1:
            for s in block.steps:
                order += 1
                out_steps.append(_executable(s, order, None))
            continue
        group += 1
        order += 1
        group_order = order
        children = []
        for s in block.steps:
            order += 1
            children.append(_executable(s, order, group))
        out_steps.append({
            "type": "RepeatGroupDTO", "stepOrder": group_order, "childStepId": group,
            "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
            "numberOfIterations": block.repeat, "smartRepeat": False,
            "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations", "displayOrder": 7,
                             "displayable": False},
            "endConditionValue": float(block.repeat), "workoutSteps": children,
        })
    return {
        "workoutName": f"hart · {steps.name}"[:60], "sportType": sport_type,
        "estimatedDurationInSecs": round(steps.minutes() * 60),
        "description": (description or "")[:1000] or None,
        "workoutSegments": [{"segmentOrder": 1, "sportType": sport_type, "workoutSteps": out_steps}],
    }


# ---------------------------------------------------------------------------
# Send / delete (job side)
# ---------------------------------------------------------------------------


def _client(db: Database, config: HartSettings) -> Any:
    from hart.ingestion.sync_manager import SyncManager
    from hart.server.jobs.scheduler import garmin_blocked

    blocked = garmin_blocked(db, datetime.datetime.now(tz=datetime.timezone.utc))
    if blocked == "garmin_auth":
        raise GarminError("Garmin login expired — run `hart auth` on the laptop")
    if blocked == "garmin_rate_limited":
        raise GarminError("Garmin is rate-limiting — try again later")
    if not config.garmin.email:
        raise GarminError("Garmin credentials missing (GARMIN_EMAIL)")
    return SyncManager(db, config).get_garmin_client()


def _row(db: Database, planned_id: int) -> dict[str, Any]:
    row = one(db, "SELECT * FROM planned_sessions WHERE id = ?", [planned_id])
    if row is None:
        raise GarminError("planned session not found")
    return row


def _remove(garmin: Any, row: dict[str, Any]) -> None:
    """Delete the previous upload; a workout already gone in Garmin is fine."""
    if row.get("garmin_workout_id"):
        try:
            garmin.delete_workout(row["garmin_workout_id"])
        except Exception as exc:  # noqa: BLE001
            if "404" not in str(exc):
                raise


def send(db: Database, planned_id: int, claude: ClaudeRunner, loop: asyncio.AbstractEventLoop,
         config: HartSettings, today: datetime.date) -> dict[str, Any]:
    row = _row(db, planned_id)
    try:
        if row["sport_type"] not in SPORTS:
            raise GarminError(f"{row['sport_type']} sessions can't be sent to Garmin yet (runs and rides only)")
        if row["date"] < today:
            raise GarminError("the session is in the past")
        steps = asyncio.run_coroutine_threadsafe(convert(claude, settings.get(db, "model_grade"), row), loop).result(timeout=240)
        workout = build_workout(row["sport_type"], steps, row.get("description"))
        garmin = _client(db, config)
        _remove(garmin, row)
        uploaded = garmin.upload_workout(workout)
        workout_id = uploaded.get("workoutId")
        if not workout_id:
            raise GarminError(f"Garmin didn't return a workout id: {str(uploaded)[:200]}")
        scheduled = garmin.schedule_workout(workout_id, row["date"].isoformat())
        schedule_id = (scheduled or {}).get("workoutScheduleId") or (scheduled or {}).get("id")
    except GarminError as exc:
        db.execute("UPDATE planned_sessions SET garmin_status = 'failed', garmin_error = ? WHERE id = ?",
                   [str(exc)[:500], planned_id])
        raise
    except Exception as exc:  # noqa: BLE001 — Garmin API errors
        from hart.server.jobs.handlers import apply_garmin_state
        from hart.server.jobs.pipeline import _classify_garmin_error

        code = _classify_garmin_error(exc)
        if code in ("garmin_auth", "garmin_rate_limited"):
            apply_garmin_state(db, code)
        message = f"Garmin: {type(exc).__name__}: {str(exc)[:300]}"
        db.execute("UPDATE planned_sessions SET garmin_status = 'failed', garmin_error = ? WHERE id = ?",
                   [message, planned_id])
        raise GarminError(message) from exc
    db.execute(
        "UPDATE planned_sessions SET garmin_steps = ?, garmin_workout_id = ?, garmin_schedule_id = ?, "
        "garmin_status = 'sent', garmin_error = NULL, garmin_sent_at = current_timestamp WHERE id = ?",
        [json.dumps(steps.model_dump()), str(workout_id), str(schedule_id) if schedule_id else None, planned_id],
    )
    return {"planned_id": planned_id, "workout_id": workout_id, "schedule_id": schedule_id,
            "minutes": round(steps.minutes()), "name": workout["workoutName"]}


def delete_remote(db: Database, config: HartSettings, workout_id: str) -> dict[str, Any]:
    garmin = _client(db, config)
    _remove(garmin, {"garmin_workout_id": workout_id})
    return {"deleted_workout": workout_id}


def auto_send(db: Database) -> bool:
    return bool(settings.get(db, "garmin_auto_send"))
