"""Plan page and suggestion API."""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from hart.server import data, garmin_workouts, plan, settings, suggestions
from hart.server.routes import get_config, get_db, page
from hart.storage.database import Database

router = APIRouter()

MAX_RANGE_DAYS = 120


class ParseIn(BaseModel):
    text: str = Field(min_length=1, max_length=plan.MAX_PASTE_CHARS)
    default_date: datetime.date | None = Field(default=None, description="Date for workouts the text doesn't date")


class ImportIn(BaseModel):
    sessions: list[plan.PlanRowIn] = Field(min_length=1, max_length=40)
    replace_existing: bool = True


class LinkIn(BaseModel):
    activity_id: str | None = None


class AutoSendIn(BaseModel):
    enabled: bool


def refresh_suggestions(request: Request, db: Database, dates: list[datetime.date]) -> list[str]:
    """Regenerate today's / tomorrow's suggestion when the coach's plan for that
    day changed after the suggestion was made (e.g. a session sent at 21:00)."""
    today = data.local_today(request.app.state.config)
    window = [d for d in dates if today <= d <= today + datetime.timedelta(days=1)]
    queued = []
    for d in suggestions.stale_for_plan(db, window):
        request.app.state.runner.enqueue(
            "suggest",
            {"date": d.isoformat(), "kind": "manual", "trigger": "plan_changed"},
            trigger="chain",
            dedupe_key=f"suggest:{d}",
        )
        queued.append(d.isoformat())
    return queued


def enqueue_garmin(request: Request, planned_id: int) -> dict[str, Any]:
    return request.app.state.runner.enqueue(
        "garmin_workout", {"planned_id": planned_id}, trigger="manual", dedupe_key=f"garmin_workout:{planned_id}"
    )


def _plan_error(exc: plan.PlanError) -> HTTPException:
    msg = str(exc)
    return HTTPException(404 if "not found" in msg else 400, detail=msg)


def _claude(request: Request) -> Any:
    claude = getattr(request.app.state, "claude", None)
    if claude is None:
        raise HTTPException(503, detail="Claude runner not available")
    return claude


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


@router.get("/plan", response_class=HTMLResponse, include_in_schema=False)
def plan_page(request: Request, db: Database = Depends(get_db), config=Depends(get_config)) -> HTMLResponse:
    today = data.local_today(config)
    return page(
        request,
        "plan.html",
        {
            "nav": "plan",
            "today": today,
            "tomorrow": today + datetime.timedelta(days=1),
            "weeks": plan.calendar(db, today),
            "suggestion": suggestions.dashboard_card(db, config),
            "garmin_auto_send": garmin_workouts.auto_send(db),
            "plan_proposals": data.rows(
                db,
                "SELECT id, kind, action, summary, reason, created_at FROM season_proposals "
                "WHERE status = 'pending' AND kind = 'plan' ORDER BY id",
            ),
            "garmin_pending": {
                int(r[0].split(":")[1])
                for r in db.fetchall(
                    "SELECT dedupe_key FROM jobs WHERE type = 'garmin_workout' AND status IN ('queued', 'running') "
                    "AND dedupe_key IS NOT NULL"
                )
            },
        },
    )


# ---------------------------------------------------------------------------
# Planned sessions
# ---------------------------------------------------------------------------


@router.get("/api/plan")
def api_plan(
    start: datetime.date | None = None,
    end: datetime.date | None = None,
    db: Database = Depends(get_db),
    config=Depends(get_config),
) -> dict[str, Any]:
    today = data.local_today(config)
    start = start or data.monday(today)
    end = end or start + datetime.timedelta(days=20)
    if end < start or (end - start).days > MAX_RANGE_DAYS:
        raise HTTPException(400, detail=f"range must be 0–{MAX_RANGE_DAYS} days")
    return {"start": start, "end": end, "items": plan.plan_rows(db, start, end)}


@router.post("/api/plan")
def api_create_plan(body: plan.PlanRowIn, request: Request, db: Database = Depends(get_db)) -> dict[str, Any]:
    new_id = plan.create_row(db, body)
    return {"id": new_id, "suggestions_refreshed": refresh_suggestions(request, db, [body.date])}


@router.put("/api/plan/{row_id}")
def api_update_plan(
    row_id: int, body: plan.PlanRowIn, request: Request, db: Database = Depends(get_db)
) -> dict[str, Any]:
    old = db.fetchone("SELECT date FROM planned_sessions WHERE id = ?", [row_id])
    try:
        plan.update_row(db, row_id, body)
    except plan.PlanError as exc:
        raise _plan_error(exc) from exc
    dates = [body.date, *([old[0]] if old else [])]
    return {"id": row_id, "suggestions_refreshed": refresh_suggestions(request, db, dates)}


@router.delete("/api/plan/{row_id}")
def api_delete_plan(row_id: int, request: Request, db: Database = Depends(get_db)) -> dict[str, Any]:
    old = db.fetchone("SELECT date FROM planned_sessions WHERE id = ?", [row_id])
    try:
        workout_id = plan.delete_row(db, row_id)
    except plan.PlanError as exc:
        raise _plan_error(exc) from exc
    refreshed = refresh_suggestions(request, db, [old[0]]) if old else []
    garmin = None
    if workout_id:
        garmin = request.app.state.runner.enqueue(
            "garmin_workout",
            {"delete_workout_id": workout_id},
            trigger="manual",
            dedupe_key=f"garmin_delete:{workout_id}",
        )
    return {"deleted": row_id, "garmin": garmin, "suggestions_refreshed": refreshed}


@router.post("/api/plan/{row_id}/send-garmin")
def api_send_garmin(row_id: int, request: Request, db: Database = Depends(get_db)) -> dict[str, Any]:
    row = data.one(db, "SELECT sport_type FROM planned_sessions WHERE id = ?", [row_id])
    if row is None:
        raise HTTPException(404, detail="planned session not found")
    if row["sport_type"] not in garmin_workouts.SPORTS:
        raise HTTPException(400, detail="only runs and rides can be sent to Garmin")
    return enqueue_garmin(request, row_id)


@router.put("/api/garmin/auto-send")
def api_garmin_auto_send(body: AutoSendIn, db: Database = Depends(get_db)) -> dict[str, Any]:
    settings.set_value(db, "garmin_auto_send", body.enabled)
    return {"enabled": body.enabled}


@router.post("/api/plan/{row_id}/link")
def api_link_plan(row_id: int, body: LinkIn, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        plan.link_activity(db, row_id, body.activity_id)
    except plan.PlanError as exc:
        raise _plan_error(exc) from exc
    return {"id": row_id, "activity_id": body.activity_id}


@router.post("/api/plan/parse")
async def api_parse_plan(body: ParseIn, request: Request) -> dict[str, Any]:
    """Pasted plan text → preview rows (nothing is saved)."""
    config = request.app.state.config
    try:
        with request.app.state.db.cursor() as cur:
            model = settings.get(cur, "model_parse")
        result = await plan.parse_paste(
            _claude(request), model, body.text, data.local_today(config), default_date=body.default_date
        )
    except plan.PlanError as exc:
        raise _plan_error(exc) from exc
    with request.app.state.db.cursor() as cur:
        dates = sorted({s["date"] for s in result["sessions"]})
        existing = (
            {
                str(r[0])
                for r in cur.fetchall(
                    "SELECT DISTINCT date FROM planned_sessions WHERE source = 'coach_import' AND date IN "
                    f"({', '.join('?' for _ in dates)})",
                    dates,
                )
            }
            if dates
            else set()
        )
    result["dates_with_existing_import"] = sorted(existing)
    return result


@router.post("/api/plan/import")
def api_import_plan(body: ImportIn, request: Request, db: Database = Depends(get_db)) -> dict[str, Any]:
    result = plan.import_rows(db, body.sessions, body.replace_existing)
    for workout_id in result.pop("garmin_workouts_removed", []):
        request.app.state.runner.enqueue(
            "garmin_workout",
            {"delete_workout_id": workout_id},
            trigger="manual",
            dedupe_key=f"garmin_delete:{workout_id}",
        )
    result["suggestions_refreshed"] = refresh_suggestions(request, db, [s.date for s in body.sessions])
    return result


@router.post("/api/plan/{row_id}/garmin")
async def api_plan_garmin(row_id: int, request: Request) -> dict[str, Any]:
    """Garmin workout text for a bike session (cached on the row)."""
    with request.app.state.db.cursor() as cur:
        row = data.one(cur, "SELECT sport_type, description, garmin_text FROM planned_sessions WHERE id = ?", [row_id])
    if row is None:
        raise HTTPException(404, detail="planned session not found")
    if row["garmin_text"]:
        return {"garmin_text": row["garmin_text"], "cached": True}
    if row["sport_type"] != "bike" or not row["description"]:
        raise HTTPException(400, detail="Garmin text is only made for bike sessions with a coach description")
    try:
        with request.app.state.db.cursor() as cur:
            model = settings.get(cur, "model_grade")
        text = await plan.garmin_text(_claude(request), model, row["description"])
    except plan.PlanError as exc:
        raise _plan_error(exc) from exc
    with request.app.state.db.cursor() as cur:
        cur.execute("UPDATE planned_sessions SET garmin_text = ? WHERE id = ?", [text, row_id])
    return {"garmin_text": text, "cached": False}


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------


@router.get("/api/suggestions/{for_date}")
def api_suggestion(for_date: datetime.date, db: Database = Depends(get_db)) -> dict[str, Any]:
    found = suggestions.for_display(db, for_date)
    if found is None:
        raise HTTPException(404, detail="no suggestion for this date")
    found["pending"] = suggestions.pending(db, for_date)
    return found


@router.post("/api/suggestions/{for_date}/regenerate")
def api_regenerate(
    for_date: datetime.date, request: Request, db: Database = Depends(get_db), config=Depends(get_config)
) -> dict[str, Any]:
    today = data.local_today(config)
    if not today - datetime.timedelta(days=1) <= for_date <= today + datetime.timedelta(days=7):
        raise HTTPException(400, detail="suggestions are made for yesterday to a week ahead")
    race = suggestions.race_on(db, for_date)
    if race:
        raise HTTPException(400, detail=f"Race day — {race['name']}: no suggestion")
    return request.app.state.runner.enqueue(
        "suggest",
        {"date": for_date.isoformat(), "kind": "manual", "trigger": "manual"},
        trigger="manual",
        dedupe_key=f"suggest:{for_date}",
    )


@router.post("/api/suggestions/{suggestion_id}/accept")
def api_accept(suggestion_id: int, request: Request, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        ids = suggestions.accept(db, suggestion_id)
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc) else 400, detail=str(exc)) from exc
    garmin = []
    if garmin_workouts.auto_send(db):
        sendable = (
            [
                r[0]
                for r in db.fetchall(
                    f"SELECT id FROM planned_sessions WHERE id IN ({', '.join('?' for _ in ids)}) AND sport_type IN ('run', 'bike')",
                    ids,
                )
            ]
            if ids
            else []
        )
        garmin = [enqueue_garmin(request, i) for i in sendable]
    return {"planned_ids": ids, "garmin": garmin}
