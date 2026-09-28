"""Health page and API: checks, lab results, lab paste import."""

from __future__ import annotations

import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from hart.server import data, health, labs, settings
from hart.server.routes import get_config, get_db, page
from hart.storage.database import Database

router = APIRouter()


class SnoozeIn(BaseModel):
    weeks: Literal[2, 4, 8] = 2


class DoneIn(BaseModel):
    done_on: datetime.date | None = None


class LabParseIn(BaseModel):
    text: str = Field(min_length=1, max_length=labs.MAX_PASTE_CHARS)


class LabImportIn(BaseModel):
    panels: list[labs.LabPanelIn] = Field(min_length=1, max_length=10)
    source: Literal["paste", "manual"] = "paste"


def _err(exc: Exception) -> HTTPException:
    msg = str(exc)
    return HTTPException(404 if "not found" in msg else 400, detail=msg)


@router.get("/health", response_class=HTMLResponse, include_in_schema=False)
def health_page(request: Request, db: Database = Depends(get_db), config=Depends(get_config)) -> HTMLResponse:
    today = data.local_today(config)
    return page(request, "health.html", {
        "nav": "health", "h": health.overview(db, today), "panels": labs.panels(db),
        "trend_series": health.trend_series(db), "catalogue": labs.CATALOGUE,
        "annotations": data.rows(db, "SELECT kind, label, start_date, end_date FROM annotations "
                                     "WHERE kind IN ('illness', 'injury') ORDER BY start_date"),
    })


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


@router.get("/api/health/checks")
def api_checks(status: str | None = None, db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    return health.checks(db, status)


@router.post("/api/health/checks")
def api_create_check(body: health.CheckIn, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        return {"id": health.create_check(db, body)}
    except health.HealthError as exc:
        raise _err(exc) from exc


@router.post("/api/health/checks/{check_id}/done")
def api_check_done(check_id: int, body: DoneIn, db: Database = Depends(get_db), config=Depends(get_config)) -> dict[str, Any]:
    try:
        return health.mark_done(db, check_id, data.local_today(config), body.done_on)
    except health.HealthError as exc:
        raise _err(exc) from exc


@router.post("/api/health/checks/{check_id}/snooze")
def api_check_snooze(check_id: int, body: SnoozeIn, db: Database = Depends(get_db), config=Depends(get_config)) -> dict[str, Any]:
    try:
        health.snooze(db, check_id, body.weeks, data.local_today(config))
    except health.HealthError as exc:
        raise _err(exc) from exc
    return {"id": check_id, "status": "snoozed"}


@router.post("/api/health/checks/{check_id}/{action}")
def api_check_action(check_id: int, action: Literal["dismiss", "approve", "reopen"],
                     db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        {"dismiss": health.dismiss, "approve": health.approve, "reopen": health.reopen}[action](db, check_id)
    except health.HealthError as exc:
        raise _err(exc) from exc
    return {"id": check_id, "action": action}


@router.post("/api/health/recheck")
def api_recheck(db: Database = Depends(get_db), config=Depends(get_config)) -> dict[str, Any]:
    return health.sync_checks(db, data.local_today(config))


# ---------------------------------------------------------------------------
# Lab results
# ---------------------------------------------------------------------------


@router.get("/api/health/labs")
def api_labs(marker: str | None = None, db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    return labs.results(db, marker)


@router.get("/api/health/markers")
def api_markers() -> list[dict[str, Any]]:
    return [{"key": m.key, "name": m.name, "category": m.category, "lab_names": list(m.aliases)}
            for m in labs.CATALOGUE]


@router.post("/api/health/labs/parse")
async def api_labs_parse(body: LabParseIn, request: Request) -> dict[str, Any]:
    claude = getattr(request.app.state, "claude", None)
    if claude is None:
        raise HTTPException(503, detail="Claude runner not available")
    try:
        with request.app.state.db.cursor() as cur:
            model = settings.get(cur, "model_parse")
        result = await labs.parse_paste(claude, model, body.text)
    except labs.LabError as exc:
        raise _err(exc) from exc
    with request.app.state.db.cursor() as cur:
        for p in result["panels"]:
            p["existing"] = cur.fetchone("SELECT count(*) FROM lab_results WHERE test_date = ?", [p["test_date"]])[0]
    return result


@router.post("/api/health/labs/import")
def api_labs_import(body: LabImportIn, db: Database = Depends(get_db), config=Depends(get_config)) -> dict[str, Any]:
    imported = [labs.import_panel(db, p, body.source) for p in body.panels]
    return {"panels": imported, "checks": health.sync_checks(db, data.local_today(config))}


@router.delete("/api/health/labs/{result_id}")
def api_labs_delete(result_id: int, db: Database = Depends(get_db), config=Depends(get_config)) -> dict[str, Any]:
    row = db.fetchone("SELECT source FROM lab_results WHERE id = ?", [result_id])
    if row is None:
        raise HTTPException(404, detail="result not found")
    if row[0] == "import":
        raise HTTPException(400, detail="results from blood_results.json can't be deleted here — edit the file")
    db.execute("DELETE FROM lab_results WHERE id = ?", [result_id])
    health.sync_checks(db, data.local_today(config))
    return {"deleted": result_id}
