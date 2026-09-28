"""Season writes: phases, races, annotations."""

from __future__ import annotations

import datetime
import json
from typing import Any

from hart.analytics.phase import PhaseSpec, generate_phases
from hart.server import state
from hart.storage.database import Database

PHASE_TYPES = ("comeback", "base", "build", "peak", "taper", "race", "transition")
ANNOTATION_KINDS = ("injury", "illness", "travel", "no_device", "event", "race", "other")
RACE_DISTANCES = ("full", "half", "olympic", "sprint", "run", "other")
RACE_PRIORITIES = ("A", "B", "C")


class SeasonError(ValueError):
    pass


def _overlaps(db: Database, start: datetime.date, end: datetime.date, exclude_id: int | None) -> list[str]:
    params: list[Any] = [end, start]
    extra = ""
    if exclude_id is not None:
        extra = " AND id <> ?"
        params.append(exclude_id)
    return [
        r[0]
        for r in db.fetchall(f"SELECT name FROM training_phases WHERE start_date <= ? AND end_date >= ?{extra}", params)
    ]


def validate_phase(db: Database, data: dict[str, Any], exclude_id: int | None = None) -> None:
    if data["phase_type"] not in PHASE_TYPES:
        raise SeasonError(f"Unknown phase type {data['phase_type']!r}")
    if data["end_date"] < data["start_date"]:
        raise SeasonError("End date is before start date")
    clash = _overlaps(db, data["start_date"], data["end_date"], exclude_id)
    if clash:
        raise SeasonError(f"Overlaps with {', '.join(clash)}")


def create_phase(db: Database, data: dict[str, Any]) -> int:
    validate_phase(db, data)
    return db.fetchone(
        "INSERT INTO training_phases (phase_type, name, start_date, end_date, goal, source, confirmed) "
        "VALUES (?, ?, ?, ?, ?, 'manual', TRUE) RETURNING id",
        [data["phase_type"], data["name"], data["start_date"], data["end_date"], data.get("goal")],
    )[0]


def update_phase(db: Database, phase_id: int, data: dict[str, Any]) -> None:
    validate_phase(db, data, exclude_id=phase_id)
    # Any edit makes the phase manual: regeneration never touches it again.
    db.execute(
        "UPDATE training_phases SET phase_type = ?, name = ?, start_date = ?, end_date = ?, goal = ?, "
        "source = 'manual', confirmed = TRUE, updated_at = current_timestamp WHERE id = ?",
        [data["phase_type"], data["name"], data["start_date"], data["end_date"], data.get("goal"), phase_id],
    )


def season_inputs(db: Database) -> tuple[datetime.date | None, datetime.date | None, datetime.date | None]:
    """A-race date, comeback start and comeback end (detected)."""
    race = db.fetchone("SELECT race_date FROM races WHERE priority = 'A' ORDER BY race_date DESC LIMIT 1")
    detection = state.get_setting(db, "season_detection") or {}
    layoff = detection.get("layoff") or {}
    start = layoff.get("return_date")
    end = detection.get("comeback_end")
    return (
        race[0] if race else None,
        datetime.date.fromisoformat(start) if start else None,
        datetime.date.fromisoformat(end) if end else None,
    )


def regenerate_phases(db: Database, today: datetime.date) -> dict[str, Any]:
    """Replace auto phases with a fresh proposal; manual phases are kept and
    generated phases that would overlap them are skipped."""
    race_date, comeback_start, comeback_end = season_inputs(db)
    if race_date is None:
        raise SeasonError("No A-race defined")
    specs: list[PhaseSpec] = generate_phases(race_date, comeback_start, comeback_end, today, state.get_thresholds(db))
    db.execute("DELETE FROM training_phases WHERE source = 'auto'")
    created, skipped = 0, []
    for spec in specs:
        if _overlaps(db, spec.start_date, spec.end_date, None):
            skipped.append(spec.name)
            continue
        db.execute(
            "INSERT INTO training_phases (phase_type, name, start_date, end_date, goal, source, confirmed) "
            "VALUES (?, ?, ?, ?, ?, 'auto', FALSE)",
            [spec.phase_type, spec.name, spec.start_date, spec.end_date, spec.goal],
        )
        created += 1
    return {"created": created, "skipped_overlapping_manual": skipped}


def seed_phases(db: Database, today: datetime.date) -> dict[str, Any] | None:
    if db.fetchone("SELECT count(*) FROM training_phases")[0] > 0:
        return None
    try:
        return regenerate_phases(db, today)
    except SeasonError:
        return None


def _plan_row(record: dict[str, Any]) -> Any:
    from pydantic import ValidationError

    from hart.server.plan import PlanRowIn

    try:
        return PlanRowIn.model_validate({k: record.get(k) for k in PLAN_FIELDS})
    except ValidationError as exc:
        raise SeasonError(f"Invalid planned session: {exc.errors()[0]['loc'][0]} — {exc.errors()[0]['msg']}") from exc


def validate_race(data: dict[str, Any]) -> None:
    if not str(data.get("name") or "").strip():
        raise SeasonError("A race needs a name")
    if data.get("distance") not in RACE_DISTANCES:
        raise SeasonError(f"distance must be one of {', '.join(RACE_DISTANCES)}")
    if data.get("priority") not in RACE_PRIORITIES:
        raise SeasonError("priority must be A, B or C")


def _next_a_race(db: Database) -> tuple[Any, ...] | None:
    return db.fetchone(
        "SELECT id, race_date FROM races WHERE priority = 'A' AND race_date >= current_date ORDER BY race_date LIMIT 1"
    )


def save_race(db: Database, data: dict[str, Any], race_id: int | None = None) -> dict[str, Any]:
    """Create or update a race.  ``phases_stale`` tells the caller the phase
    plan (built backwards from the next A-race) no longer matches it."""
    validate_race(data)
    before = _next_a_race(db)
    values = [data["name"].strip(), data["race_date"], data["distance"], data["priority"], data.get("notes") or None]
    if race_id is None:
        race_id = db.fetchone(
            "INSERT INTO races (name, race_date, distance, priority, notes) VALUES (?, ?, ?, ?, ?) RETURNING id", values
        )[0]
    else:
        if db.fetchone("SELECT 1 FROM races WHERE id = ?", [race_id]) is None:
            raise SeasonError("Race not found")
        db.execute(
            "UPDATE races SET name = ?, race_date = ?, distance = ?, priority = ?, notes = ? WHERE id = ?",
            [*values, race_id],
        )
    return {"id": race_id, "phases_stale": _next_a_race(db) != before}


def delete_race(db: Database, race_id: int) -> dict[str, Any]:
    if db.fetchone("SELECT 1 FROM races WHERE id = ?", [race_id]) is None:
        raise SeasonError("Race not found")
    before = _next_a_race(db)
    db.execute("DELETE FROM races WHERE id = ?", [race_id])
    return {"deleted": race_id, "phases_stale": _next_a_race(db) != before}


def validate_annotation(data: dict[str, Any]) -> None:
    if data["kind"] not in ANNOTATION_KINDS:
        raise SeasonError(f"Unknown annotation kind {data['kind']!r}")
    if data.get("end_date") and data["end_date"] < data["start_date"]:
        raise SeasonError("End date is before start date")


# ---------------------------------------------------------------------------
# Proposals from Ember (Claude proposes, the athlete applies)
# ---------------------------------------------------------------------------

PHASE_FIELDS = ("phase_type", "name", "start_date", "end_date", "goal")
ANNOTATION_FIELDS = ("kind", "label", "start_date", "end_date")
RACE_FIELDS = ("name", "race_date", "distance", "priority", "notes")
PLAN_FIELDS = ("date", "sport_type", "title", "duration_min", "intensity", "description")
# kind → (table, fields, optional fields, date fields)
PROPOSAL_KINDS: dict[str, tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "phase": ("training_phases", PHASE_FIELDS, ("goal",), ("start_date", "end_date")),
    "annotation": ("annotations", ANNOTATION_FIELDS, ("end_date",), ("start_date", "end_date")),
    "race": ("races", RACE_FIELDS, ("notes",), ("race_date",)),
    "plan": ("planned_sessions", PLAN_FIELDS, ("duration_min", "intensity", "description"), ("date",)),
}


def _current(db: Database, kind: str, target_id: int) -> dict[str, Any] | None:
    table, fields, _, _ = PROPOSAL_KINDS[kind]
    row = db.fetchone(f"SELECT {', '.join(fields)} FROM {table} WHERE id = ?", [target_id])
    return dict(zip(fields, row)) if row else None


def _merged(db: Database, kind: str, action: str, target_id: int | None, payload: dict[str, Any]) -> dict[str, Any]:
    """The full record the proposal would produce (validated like a manual edit)."""
    _, fields, optional, dates = PROPOSAL_KINDS[kind]
    base: dict[str, Any] = {}
    if action == "update":
        current = _current(db, kind, target_id or 0)
        if current is None:
            raise SeasonError(f"No {kind} with id {target_id}")
        base = current
    record = {**base, **{k: v for k, v in payload.items() if k in fields and v not in (None, "")}}
    for key in dates:
        if isinstance(record.get(key), str):
            try:
                record[key] = datetime.date.fromisoformat(record[key])
            except ValueError as exc:
                raise SeasonError(f"{key} must be YYYY-MM-DD") from exc
    missing = [f for f in fields if f not in optional and not record.get(f)]
    if missing:
        raise SeasonError(f"Missing: {', '.join(missing)}")
    return record


def _describe(db: Database, kind: str, action: str, target_id: int | None, record: dict[str, Any]) -> str:
    noun = kind
    fields = PROPOSAL_KINDS[kind][1]

    def span(r: dict[str, Any]) -> str:
        if kind == "plan":
            minutes = f" {r.get('duration_min')}′" if r.get("duration_min") else ""
            return f"{r.get('date')}, {r.get('sport_type')}{minutes}"
        if kind == "race":
            return f"{r.get('race_date')}, {r.get('distance')}, {r.get('priority')}-race"
        return f"{r.get('start_date')} → {r.get('end_date') or 'ongoing'}"

    noun = "planned session" if kind == "plan" else noun
    name = record.get("name") or record.get("label") or record.get("title")
    if action == "create":
        extra = "" if kind in ("race", "plan") else f" ({record.get('phase_type') or record.get('kind')})"
        return f"Add {noun} “{name}”{extra}: {span(record)}"
    current = _current(db, kind, target_id or 0) or {}
    old_name = current.get("name") or current.get("label") or current.get("title")
    if action == "delete":
        return f"Delete {noun} “{old_name}” ({span(current)})"
    changes = [
        f"{k.replace('_', ' ')} {current.get(k)} → {record.get(k)}"
        for k in fields
        if str(current.get(k)) != str(record.get(k))
    ]
    return f"Change {noun} “{old_name}”: " + ("; ".join(changes) if changes else "no changes")


def create_proposal(
    db: Database, kind: str, action: str, reason: str, target_id: int | None, payload: dict[str, Any]
) -> dict[str, Any]:
    if kind not in PROPOSAL_KINDS:
        raise SeasonError("kind must be 'phase', 'annotation', 'race' or 'plan'")
    if action not in ("create", "update", "delete"):
        raise SeasonError("action must be create, update or delete")
    if not reason.strip():
        raise SeasonError("A reason is required")
    if action == "delete":
        if _current(db, kind, target_id or 0) is None:
            raise SeasonError(f"No {kind} with id {target_id}")
        record: dict[str, Any] = {}
    else:
        record = _merged(db, kind, action, target_id, payload)
        if kind == "phase":
            if record["phase_type"] not in PHASE_TYPES:
                raise SeasonError(f"phase_type must be one of {', '.join(PHASE_TYPES)}")
            if record["end_date"] < record["start_date"]:
                raise SeasonError("End date is before start date")
            clash = _overlaps(db, record["start_date"], record["end_date"], target_id if action == "update" else None)
            if clash:
                raise SeasonError(
                    f"Overlaps with {', '.join(clash)} — propose shortening or deleting those phases first"
                )
        elif kind == "race":
            validate_race(record)
        elif kind == "plan":
            _plan_row(record)
        else:
            validate_annotation(record)
    summary = _describe(db, kind, action, target_id, record)
    stored = {
        k: (v.isoformat() if isinstance(v, datetime.date) else v) for k, v in payload.items() if v not in (None, "")
    }
    new_id = db.fetchone(
        "INSERT INTO season_proposals (kind, action, target_id, payload, reason, summary, status, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', 'claude') RETURNING id",
        [kind, action, target_id, json.dumps(stored), reason.strip(), summary],
    )[0]
    return {"id": new_id, "status": "pending", "summary": summary}


def apply_proposal(db: Database, proposal_id: int) -> dict[str, Any]:
    row = db.fetchone(
        "SELECT kind, action, target_id, payload, status FROM season_proposals WHERE id = ?", [proposal_id]
    )
    if row is None:
        raise SeasonError("Proposal not found")
    kind, action, target_id, payload, status = row
    if status != "pending":
        raise SeasonError(f"Proposal is already {status}")
    payload = json.loads(payload) if isinstance(payload, str) else (payload or {})
    table = PROPOSAL_KINDS[kind][0]
    phases_stale = False
    extra: dict[str, Any] = {}
    if kind == "plan":
        from hart.server import plan

        current = _current(db, kind, target_id or 0) if action != "create" else None
        extra["plan_dates"] = [d for d in {(current or {}).get("date"), payload.get("date")} if d]
        if action == "delete":
            extra["garmin_workout_removed"] = plan.delete_row(db, target_id)
        elif action == "create":
            extra["planned_id"] = plan.create_row(
                db, _plan_row(_merged(db, kind, action, target_id, payload)), source="manual"
            )
        else:
            plan.update_row(db, target_id, _plan_row(_merged(db, kind, action, target_id, payload)))
    elif action == "delete":
        if kind == "race":
            phases_stale = delete_race(db, target_id)["phases_stale"]
        else:
            db.execute(f"DELETE FROM {table} WHERE id = ?", [target_id])
    elif kind == "race":
        record = _merged(db, kind, action, target_id, payload)
        phases_stale = save_race(db, record, target_id if action == "update" else None)["phases_stale"]
    else:
        record = _merged(db, kind, action, target_id, payload)
        if kind == "phase":
            if action == "create":
                create_phase(db, record)
            else:
                update_phase(db, target_id, record)
        else:
            validate_annotation(record)
            if action == "create":
                db.execute(
                    "INSERT INTO annotations (kind, label, start_date, end_date, source) VALUES (?, ?, ?, ?, 'manual')",
                    [record["kind"], record["label"], record["start_date"], record.get("end_date")],
                )
            else:
                db.execute(
                    "UPDATE annotations SET kind = ?, label = ?, start_date = ?, end_date = ?, source = 'manual' WHERE id = ?",
                    [record["kind"], record["label"], record["start_date"], record.get("end_date"), target_id],
                )
    db.execute(
        "UPDATE season_proposals SET status = 'applied', decided_at = current_timestamp WHERE id = ?", [proposal_id]
    )
    return {"id": proposal_id, "status": "applied", "phases_stale": phases_stale, "kind": kind, **extra}


def dismiss_proposal(db: Database, proposal_id: int) -> dict[str, Any]:
    db.execute(
        "UPDATE season_proposals SET status = 'dismissed', decided_at = current_timestamp "
        "WHERE id = ? AND status = 'pending'",
        [proposal_id],
    )
    return {"id": proposal_id, "status": "dismissed"}
