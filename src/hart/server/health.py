"""Health checks: store and sync the reminder rules.

``sync_checks`` runs the deterministic rules and reconciles ``health_checks``:
new rule keys become open checks, open rule checks follow their rule's
current due date and rationale, and a rule check that is no longer produced
was resolved — lab checks by newer results (marked done with the lab date),
anything else no longer applies (dismissed).  Dismissed and done checks are
never recreated for the same key.
"""

from __future__ import annotations

import datetime
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from hart.analytics.health_checks import DEFAULTS, Context, CheckSpec, evaluate
from hart.server import labs, state
from hart.server.data import one, rows
from hart.storage.database import Database

D = datetime.date
KINDS = ("lab_panel", "follow_up", "medical_exam", "physio", "other")
LAB_KINDS = ("lab_panel", "follow_up")
UPCOMING_DAYS = 90
HEALTH_SETTINGS_KEY = "health_thresholds"

Kind = Literal["lab_panel", "follow_up", "medical_exam", "physio", "other"]


def thresholds(db: Database) -> dict[str, float]:
    overrides = state.get_setting(db, HEALTH_SETTINGS_KEY, {}) or {}
    return {**DEFAULTS, **{k: v for k, v in overrides.items() if k in DEFAULTS}}


def _names() -> dict[str, str]:
    return {m.key: m.name for m in labs.CATALOGUE}


def context(db: Database, today: D) -> Context:
    notes = rows(
        db,
        "SELECT id, category, title, body, rules, valid_from, CAST(created_at AS DATE) AS created FROM athlete_notes "
        "WHERE status = 'active' AND (valid_to IS NULL OR valid_to >= ?)",
        [today],
    )
    for n in notes:
        if isinstance(n.get("rules"), str):
            n["rules"] = json.loads(n["rules"])
    physio_done = {}
    for r in rows(db, "SELECT rule_key, done_on FROM health_checks WHERE kind = 'physio' AND status = 'done' "
                      "AND rule_key IS NOT NULL ORDER BY done_on"):
        physio_done[int(r["rule_key"].split(":")[1])] = r["done_on"]
    build = one(db, "SELECT min(start_date) AS d FROM training_phases WHERE phase_type = 'build' AND start_date > ?",
                [today])
    race = one(db, "SELECT name, race_date, distance FROM races WHERE priority = 'A' AND race_date > ? "
                   "ORDER BY race_date LIMIT 1", [today])
    return Context(
        today=today, results=labs.results(db), names=_names(), build_start=build["d"] if build else None,
        a_race=race, notes=notes, physio_done=physio_done, t=thresholds(db),
    )


def _latest_lab_date(db: Database, markers: list[str], after: D | None) -> D | None:
    if not markers:
        return None
    placeholders = ", ".join("?" for _ in markers)
    found = db.fetchone(
        f"SELECT max(test_date) FROM lab_results WHERE marker_key IN ({placeholders}) AND test_date > coalesce(?, DATE '1900-01-01')",
        [*markers, after],
    )
    return found[0] if found else None


def _anchor(rule_key: str, created: D) -> D:
    """Results after this date resolve a lab check: the test date in keys like
    "followup:2026-05-07" or "panel:2026-05-07", else the day it was created."""
    try:
        d = D.fromisoformat(rule_key.rsplit(":", 1)[-1])
    except ValueError:
        return created - datetime.timedelta(days=1)
    return d if d <= created else created - datetime.timedelta(days=1)


def sync_checks(db: Database, today: D) -> dict[str, int]:
    """Reconcile stored checks with the rules; returns counts of changes."""
    specs, _ = evaluate(context(db, today))
    wanted: dict[str, CheckSpec] = {s.rule_key: s for s in specs}
    counts = {"created": 0, "updated": 0, "resolved": 0, "reopened": 0}

    # Snoozes that ran out are due again.
    counts["reopened"] = db.fetchone(
        "SELECT count(*) FROM health_checks WHERE status = 'snoozed' AND snoozed_until <= ?", [today])[0]
    db.execute("UPDATE health_checks SET status = 'open', snoozed_until = NULL, updated_at = current_timestamp "
               "WHERE status = 'snoozed' AND snoozed_until <= ?", [today])

    existing = {r["rule_key"]: r for r in rows(
        db, "SELECT id, rule_key, status, kind, markers, due_date, rationale, created_at FROM health_checks "
            "WHERE source = 'rule' AND rule_key IS NOT NULL")}
    for key, spec in wanted.items():
        row = existing.get(key)
        if row is None:
            db.execute(
                "INSERT INTO health_checks (kind, title, markers, due_date, status, source, rule_key, rationale) "
                "VALUES (?, ?, ?, ?, 'open', 'rule', ?, ?)",
                [spec.kind, spec.title, json.dumps(spec.markers), spec.due_date, key, spec.rationale],
            )
            counts["created"] += 1
        elif row["status"] in ("open", "snoozed") and (
                row["due_date"] != spec.due_date or row["rationale"] != spec.rationale):
            db.execute("UPDATE health_checks SET title = ?, markers = ?, due_date = ?, rationale = ?, "
                       "updated_at = current_timestamp WHERE id = ?",
                       [spec.title, json.dumps(spec.markers), spec.due_date, spec.rationale, row["id"]])
            counts["updated"] += 1
    for key, row in existing.items():
        if key in wanted or row["status"] not in ("open", "snoozed"):
            continue
        markers = json.loads(row["markers"]) if isinstance(row["markers"], str) else (row["markers"] or [])
        lab_date = _latest_lab_date(db, markers, _anchor(key, row["created_at"].date())) \
            if row["kind"] in LAB_KINDS else None
        if lab_date:
            db.execute("UPDATE health_checks SET status = 'done', done_on = ?, lab_date = ?, "
                       "updated_at = current_timestamp WHERE id = ?", [lab_date, lab_date, row["id"]])
        else:
            db.execute("UPDATE health_checks SET status = 'dismissed', rationale = rationale || ' (No longer applies.)', "
                       "updated_at = current_timestamp WHERE id = ?", [row["id"]])
        counts["resolved"] += 1
    return counts


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


class CheckIn(BaseModel):
    kind: Kind = "other"
    title: str = Field(min_length=1, max_length=120)
    due_date: D | None = None
    markers: list[str] = Field(default_factory=list, max_length=30)
    interval_days: int | None = Field(default=None, ge=7, le=1100)
    rationale: str | None = Field(default=None, max_length=1500)


class HealthError(Exception):
    pass


def create_check(db: Database, body: CheckIn, source: str = "manual") -> int:
    unknown = [m for m in body.markers if m not in labs.BY_KEY]
    if unknown:
        raise HealthError(f"unknown marker keys: {', '.join(unknown)} (use the catalogue keys, e.g. ferritin, tsh)")
    status = "proposed" if source == "claude_proposed" else "open"
    return db.fetchone(
        "INSERT INTO health_checks (kind, title, markers, due_date, interval_days, status, source, rationale) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        [body.kind, body.title.strip(), json.dumps(body.markers), body.due_date, body.interval_days, status, source,
         (body.rationale or "").strip() or None],
    )[0]


def _get(db: Database, check_id: int) -> dict[str, Any]:
    found = one(db, "SELECT * FROM health_checks WHERE id = ?", [check_id])
    if found is None:
        raise HealthError("health check not found")
    return found


def mark_done(db: Database, check_id: int, today: D, done_on: D | None = None) -> dict[str, Any]:
    check = _get(db, check_id)
    if check["status"] in ("done", "dismissed"):
        raise HealthError(f"already {check['status']}")
    done_on = done_on or today
    db.execute("UPDATE health_checks SET status = 'done', done_on = ?, snoozed_until = NULL, "
               "updated_at = current_timestamp WHERE id = ?", [done_on, check_id])
    next_id = None
    if check["interval_days"]:
        next_id = db.fetchone(
            "INSERT INTO health_checks (kind, title, markers, due_date, interval_days, status, source, rationale) "
            "VALUES (?, ?, ?, ?, ?, 'open', ?, ?) RETURNING id",
            [check["kind"], check["title"], check["markers"], done_on + datetime.timedelta(days=check["interval_days"]),
             check["interval_days"], check["source"], check["rationale"]],
        )[0]
    sync_checks(db, today)  # e.g. a finished physio check-in schedules the next one
    return {"id": check_id, "next_id": next_id}


def snooze(db: Database, check_id: int, weeks: int, today: D) -> None:
    check = _get(db, check_id)
    if check["status"] not in ("open", "snoozed"):
        raise HealthError(f"can't snooze a {check['status']} check")
    db.execute("UPDATE health_checks SET status = 'snoozed', snoozed_until = ?, updated_at = current_timestamp "
               "WHERE id = ?", [today + datetime.timedelta(weeks=weeks), check_id])


def dismiss(db: Database, check_id: int) -> None:
    _get(db, check_id)
    db.execute("UPDATE health_checks SET status = 'dismissed', updated_at = current_timestamp WHERE id = ?", [check_id])


def approve(db: Database, check_id: int) -> None:
    check = _get(db, check_id)
    if check["status"] != "proposed":
        raise HealthError("only proposed checks can be approved")
    db.execute("UPDATE health_checks SET status = 'open', updated_at = current_timestamp WHERE id = ?", [check_id])


def reopen(db: Database, check_id: int) -> None:
    _get(db, check_id)
    db.execute("UPDATE health_checks SET status = 'open', done_on = NULL, snoozed_until = NULL, "
               "updated_at = current_timestamp WHERE id = ?", [check_id])


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _decode(c: dict[str, Any]) -> dict[str, Any]:
    if isinstance(c.get("markers"), str):
        c["markers"] = json.loads(c["markers"])
    c["marker_names"] = [labs.BY_KEY[m].name if m in labs.BY_KEY else m for m in c.get("markers") or []]
    return c


def checks(db: Database, status: str | None = None) -> list[dict[str, Any]]:
    where, params = ("WHERE status = ?", [status]) if status else ("", [])
    return [_decode(c) for c in rows(
        db, f"SELECT * FROM health_checks {where} ORDER BY due_date NULLS LAST, id", params)]


def overview(db: Database, today: D) -> dict[str, Any]:
    all_checks = checks(db)
    active = [c for c in all_checks if c["status"] == "open"]
    for c in active:
        c["days"] = (c["due_date"] - today).days if c["due_date"] else None
    due = [c for c in active if c["due_date"] is None or c["due_date"] <= today]
    upcoming = [c for c in active if c["due_date"] and today < c["due_date"] <= today + datetime.timedelta(days=UPCOMING_DAYS)]
    later = [c for c in active if c["due_date"] and c["due_date"] > today + datetime.timedelta(days=UPCOMING_DAYS)]
    _, trend_list = evaluate(context(db, today))
    return {
        "today": today, "due": due, "upcoming": upcoming, "later": later,
        "proposed": [c for c in all_checks if c["status"] == "proposed"],
        "snoozed": [c for c in all_checks if c["status"] == "snoozed"],
        "closed": [c for c in all_checks if c["status"] in ("done", "dismissed")][-20:][::-1],
        "trends": trend_list,
    }


def overdue_count(db: Database, today: D) -> int:
    return db.fetchone("SELECT count(*) FROM health_checks WHERE status = 'open' AND due_date <= ?", [today])[0]


def trend_series(db: Database) -> dict[str, Any]:
    """Per key marker: dated values with the lab range, for the trend charts."""
    out = {}
    for key in labs.TREND_MARKERS:
        rs = [r for r in labs.results(db, key) if r["value_num"] is not None]
        if not rs:
            continue
        out[key] = {
            "name": labs.BY_KEY[key].name, "unit": rs[-1]["unit"],
            "points": [{"date": r["test_date"], "value": r["value_num"], "text": r["value_text"],
                        "low": r["ref_low"], "high": r["ref_high"], "status": r["status"]} for r in rs],
        }
    return out
