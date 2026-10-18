"""Join recordings of one session into one activity, and split them back.

The first recording keeps its activity_id (ids come from the start time), so its note, RPE, grades and plan
link carry over. The later recordings' streams, laps, R-R intervals, workout steps and strength sets move to
it, then they are removed; ``activity_joins`` keeps their Garmin ids so a sync doesn't bring them back.
Garmin keeps every original recording, which is how splitting back works: the joined data is cleared and the
recordings are downloaded again (``SyncManager.split_back``), and they are never joined automatically again.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from hart.analytics.joins import combine_activity, combine_metrics, join_problem, joined_description
from hart.ingestion.decoupling import activity_decoupling
from hart.ingestion.grade_adjusted import activity_gap_metrics, is_outdoor_run
from hart.storage.database import Database
from hart.storage.writers import delete_activity_rows

logger = logging.getLogger(__name__)

# Rows that describe one recording, keyed by activity_id.
SAMPLE_TABLES = ("activity_streams", "activity_laps", "activity_workout_steps", "hrv_samples", "strength_sets")
# Indexed rows: a later part's rows go after the earlier ones.
_INDEXED = {"activity_laps": "lap_index", "hrv_samples": "sample_index", "strength_sets": "set_index"}
_STEP_COLUMNS = (
    "step_index, name, intensity, duration_type, duration_value, target_type, target_low, target_high, "
    "target_unit, repeat_from, repeat_count, notes"
)


class JoinError(ValueError):
    """The activities can't be joined or split (the message says why)."""


def _rows(db: Database, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    cur = db.connection.execute(sql, params or [])
    names = [c[0] for c in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _metrics(db: Database, activity_id: str) -> dict[str, Any] | None:
    found = _rows(db, "SELECT * FROM activity_metrics WHERE activity_id = ?", [activity_id])
    if not found:
        return None
    row = found[0]
    if isinstance(row.get("hr_zone_seconds"), str):
        row["hr_zone_seconds"] = json.loads(row["hr_zone_seconds"])
    return row


def load_parts(db: Database, activity_ids: list[str]) -> list[dict[str, Any]]:
    """The activities to join, by start time; :class:`JoinError` if one is missing or they don't fit."""
    parts = []
    for activity_id in dict.fromkeys(activity_ids):
        found = _rows(db, "SELECT * FROM activities WHERE activity_id = ?", [activity_id])
        if not found:
            raise JoinError(f"Activity {activity_id} not found.")
        parts.append(found[0])
    parts.sort(key=lambda p: p["start_time"])
    problem = join_problem(parts)
    if problem:
        raise JoinError(problem)
    return parts


def joined_parts(db: Database, activity_id: str) -> list[dict[str, Any]]:
    """The recordings joined into *activity_id* besides its own first one, by start time."""
    return _rows(
        db,
        "SELECT activity_id, source, external_id, start_time, method FROM activity_joins "
        "WHERE joined_into = ? ORDER BY start_time",
        [activity_id],
    )


def garmin_ids(db: Database, activity_id: str) -> list[str]:
    """Garmin ids of every recording in the activity, the first one first — one for an activity not joined."""
    row = db.fetchone("SELECT external_id FROM activities WHERE activity_id = ? AND source = 'garmin'", [activity_id])
    if row is None or row[0] is None:
        return []
    return [str(row[0])] + [str(p["external_id"]) for p in joined_parts(db, activity_id) if p["external_id"]]


def joined_garmin_ids(db: Database) -> set[str]:
    """Garmin ids of recordings joined into another activity: a sync must not import them again."""
    return {
        str(r[0])
        for r in db.fetchall(
            "SELECT external_id FROM activity_joins WHERE source = 'garmin' AND joined_into IS NOT NULL "
            "AND external_id IS NOT NULL"
        )
    }


def is_joined(db: Database, activity_id: str) -> bool:
    return db.fetchone("SELECT 1 FROM activity_joins WHERE joined_into = ? LIMIT 1", [activity_id]) is not None


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------


def join_activities(db: Database, activity_ids: list[str], method: str) -> dict[str, Any]:
    """Join the activities into the earliest one (see the module docstring); *method* is auto or manual.

    Returns the joined ``activity_id`` and the ids of the ``parts`` that went into it.
    """
    parts = load_parts(db, activity_ids)
    target, rest = parts[0]["activity_id"], [p["activity_id"] for p in parts[1:]]
    ids = [p["activity_id"] for p in parts]
    joined = combine_activity(parts)
    metrics = combine_metrics(parts, [_metrics(db, a) for a in ids], joined)
    with db.transaction():
        for part in rest:
            _move_samples(db, target, part)
        _merge_feedback(db, parts, joined)
        _relink_plan(db, target, rest)
        columns = [c for c in joined if c not in ("activity_id", "imported_at")]
        db.execute(
            f"UPDATE activities SET {', '.join(f'{c} = ?' for c in columns)} WHERE activity_id = ?",
            [*(joined[c] for c in columns), target],
        )
        placeholders = ", ".join("?" for _ in rest)
        # A part that was itself a join brings its recordings along; the first part isn't "kept apart" any more.
        db.execute(f"UPDATE activity_joins SET joined_into = ? WHERE joined_into IN ({placeholders})", [target, *rest])
        db.execute("DELETE FROM activity_joins WHERE activity_id = ?", [target])
        for p in parts[1:]:
            delete_activity_rows(db, p["activity_id"])
            db.execute(
                "INSERT OR REPLACE INTO activity_joins (activity_id, source, external_id, start_time, joined_into, "
                "method) VALUES (?, ?, ?, ?, ?, ?)",
                [p["activity_id"], p["source"], p["external_id"], p["start_time"], target, method],
            )
        metrics["aerobic_decoupling_pct"] = activity_decoupling(db, target, joined["sport_type"])
        if is_outdoor_run(joined["sport_type"], joined.get("sub_type")):
            metrics.update(
                activity_gap_metrics(db, target, joined.get("avg_pace_sec_km"), metrics.get("efficiency_factor"))
            )
        _write_metrics(db, metrics)
    logger.info("Joined %s into %s (%s)", rest, target, method)
    return {"activity_id": target, "parts": rest}


def _move_samples(db: Database, target: str, part: str) -> None:
    """Give *part*'s streams, laps, R-R intervals, sets and workout to *target* (its own rows are removed later)."""
    # Streams are keyed by absolute time, so the parts never collide.
    db.execute(
        "INSERT OR IGNORE INTO activity_streams BY NAME "
        "SELECT * REPLACE (? AS activity_id) FROM activity_streams WHERE activity_id = ?",
        [target, part],
    )
    # The workout: the first part's; a later one's only if the first had none. Laps keep their step only
    # when it is the same workout.
    steps = {
        t: db.fetchall(
            f"SELECT {_STEP_COLUMNS} FROM activity_workout_steps WHERE activity_id = ? ORDER BY step_index", [t]
        )
        for t in (target, part)
    }
    if steps[part] and not steps[target]:
        db.execute(
            f"INSERT INTO activity_workout_steps (activity_id, {_STEP_COLUMNS}) "
            f"SELECT ?, {_STEP_COLUMNS} FROM activity_workout_steps WHERE activity_id = ?",
            [target, part],
        )
    same_workout = not steps[target] or steps[target] == steps[part]
    for table, index in _INDEXED.items():
        offset = db.fetchone(f"SELECT coalesce(max({index}) + 1, 0) FROM {table} WHERE activity_id = ?", [target])[0]
        replace = f"? AS activity_id, {index} + ? AS {index}"
        if table == "activity_laps" and not same_workout:
            replace += ", NULL AS wkt_step_index"
        db.execute(
            f"INSERT INTO {table} BY NAME SELECT * REPLACE ({replace}) FROM {table} WHERE activity_id = ?",
            [target, offset, part],
        )


def _merge_feedback(db: Database, parts: list[dict[str, Any]], joined: dict[str, Any]) -> None:
    """One note and effort for the joined session: the last RPE/feel given; the parts' notes in order."""
    ids = [p["activity_id"] for p in parts]
    found = {
        r["activity_id"]: r
        for r in _rows(
            db,
            f"SELECT * FROM session_feedback WHERE activity_id IN ({', '.join('?' for _ in ids)})",
            ids,
        )
    }
    if not set(found) - {ids[0]}:
        return  # nothing in hart about the later parts; a note following Garmin follows the joined description
    rows = [found.get(a) or {} for a in ids]
    rpe = next((r["rpe"] for r in reversed(rows) if r.get("rpe") is not None), None)
    feel = next((r["feel"] for r in reversed(rows) if r.get("feel") is not None), None)
    comment = seen = None
    if any(r.get("comment") is not None for r in rows):
        # Written in hart for some part: each part's own note (or its Garmin description), in order.
        notes = [
            {"note": r["comment"] if r.get("comment") is not None else p.get("description")}
            for p, r in zip(parts, rows)
        ]
        comment = joined_description(notes, "note") or ""
        seen = joined["description"]
    db.execute(
        "INSERT OR REPLACE INTO session_feedback (activity_id, rpe, feel, comment, note_garmin_seen, updated_at) "
        "VALUES (?, ?, ?, ?, ?, current_timestamp)",
        [ids[0], rpe, feel, comment, seen],
    )


def _relink_plan(db: Database, target: str, rest: list[str]) -> None:
    """A plan session you linked to a later part by hand now points at the joined session; automatic links are
    dropped (the caller re-runs plan matching for the day)."""
    placeholders = ", ".join("?" for _ in rest)
    db.execute(
        f"UPDATE planned_sessions SET activity_id = ? WHERE activity_id IN ({placeholders}) "
        "AND match_method = 'manual'",
        [target, *rest],
    )
    db.execute(
        f"UPDATE planned_sessions SET activity_id = NULL, match_method = NULL WHERE activity_id IN ({placeholders})",
        rest,
    )


def _write_metrics(db: Database, metrics: dict[str, Any]) -> None:
    row = dict(metrics)
    if row.get("hr_zone_seconds") is not None:
        row["hr_zone_seconds"] = json.dumps(row["hr_zone_seconds"])
    db.execute(
        f"INSERT OR REPLACE INTO activity_metrics ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
        list(row.values()),
    )


# ---------------------------------------------------------------------------
# Split back
# ---------------------------------------------------------------------------


def split_parts(db: Database, activity_id: str) -> list[dict[str, Any]]:
    """The recordings of a joined activity to download again, the first one first."""
    first = _rows(
        db, "SELECT activity_id, source, external_id, start_time FROM activities WHERE activity_id = ?", [activity_id]
    )
    if not first:
        raise JoinError("Activity not found.")
    parts = joined_parts(db, activity_id)
    if not parts:
        raise JoinError("This activity wasn't joined from parts.")
    everything = first + parts
    if any(p["source"] != "garmin" or not p["external_id"] for p in everything):
        raise JoinError("Only activities from Garmin can be split back — the parts are downloaded again.")
    return everything


def clear_joined(db: Database, activity_id: str, parts: list[dict[str, Any]]) -> None:
    """Before the recordings are stored again: drop the joined samples and metrics (the note, grades and a plan
    link made by hand stay with the first recording) and mark every recording as kept apart."""
    for table in (*SAMPLE_TABLES, "activity_metrics", "anomaly_log"):
        db.execute(f"DELETE FROM {table} WHERE activity_id = ?", [activity_id])
    db.execute(
        "UPDATE planned_sessions SET activity_id = NULL, match_method = NULL "
        "WHERE activity_id = ? AND match_method = 'auto'",
        [activity_id],
    )
    for p in parts:
        db.execute(
            "INSERT OR REPLACE INTO activity_joins (activity_id, source, external_id, start_time, joined_into, method) "
            "VALUES (?, ?, ?, ?, NULL, 'split')",
            [p["activity_id"], p["source"], p["external_id"], p["start_time"]],
        )
