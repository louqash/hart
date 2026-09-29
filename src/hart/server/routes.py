"""Web pages and JSON API: dashboard, fitness, season, sessions, notes, strength."""

from __future__ import annotations

import datetime
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup
from pydantic import BaseModel, Field, field_validator

from hart.config import HartSettings
from hart.server import data, season_ops, state
from hart.storage.database import Database

WEB_DIR = Path(__file__).resolve().parent / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
_md = MarkdownIt("commonmark", {"html": False}).enable("table")

router = APIRouter()

SportFilter = Literal["combined", "swim", "bike", "run", "strength", "other"]
RangeKey = Literal["90d", "180d", "1y", "all"]
NOTE_CATEGORIES = ("injury", "constraint", "baseline", "preference", "goal", "health", "equipment", "other")
RULE_KEYS = {
    "forbid_sports",
    "max_duration_min",
    "max_intensity",
    "allowed_weekdays",
    "max_sessions_per_week",
    "planned_labs",
    "min_sessions_per_week",
    "preferred_weekdays",
}


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------


def fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    minutes = round(seconds / 60)
    return f"{minutes // 60}:{minutes % 60:02d}"


def fmt_pace(sec_per_km: float | None) -> str:
    if not sec_per_km:
        return "—"
    return f"{int(sec_per_km // 60)}:{int(round(sec_per_km % 60)):02d}/km"


def fmt_num(value: float | None, digits: int = 0, unit: str = "") -> str:
    if value is None:
        return "—"
    text = f"{value:.{digits}f}"
    return f"{text} {unit}".strip()


def fmt_date(value: Any, pattern: str = "%a %d %b") -> str:
    if value is None:
        return "—"
    return value.strftime(pattern)


def markdown(text: str | None) -> Markup:
    return Markup(_md.render(text or ""))  # html=False: raw HTML in notes is escaped


SPORT_LABELS = {"swim": "Swim", "bike": "Bike", "run": "Run", "strength": "Strength", "other": "Other"}


def sport_label(sport: str | None, sub_type: str | None = None) -> str:
    label = SPORT_LABELS.get(sport or "", (sport or "?").title())
    if data.is_indoor(sub_type):
        label += " · indoor"
    return label


for name, fn in {
    "duration": fmt_duration,
    "pace": fmt_pace,
    "num": fmt_num,
    "date": fmt_date,
    "markdown": markdown,
    "sport": sport_label,
}.items():
    templates.env.filters[name] = fn


def static_url(path: str) -> str:
    """/static URL with a version query, so deploys never serve stale cached JS/CSS."""
    file = WEB_DIR / "static" / path
    version = int(file.stat().st_mtime) if file.is_file() else 0
    return f"/static/{path}?v={version}"


templates.env.globals["static"] = static_url
from hart.server.glossary import GLOSSARY  # noqa: E402

templates.env.globals["GLOSSARY"] = GLOSSARY
# `|tojson` HTML-escapes (safe in attributes and <script>); dates become ISO strings.
templates.env.policies["json.dumps_kwargs"] = {"sort_keys": True, "default": str}


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_db(request: Request) -> Iterator[Database]:
    cur = request.app.state.db.cursor()
    try:
        yield cur
    finally:
        cur.close()


def get_config(request: Request) -> HartSettings:
    return request.app.state.config


def page(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    from hart.server.claude.prompts import athlete_profile

    with request.app.state.db.cursor() as cur:
        header = data.header_status(cur, request.app.state.config)
        prefs = athlete_profile(cur)
    return templates.TemplateResponse(
        request, name, {"login": request.state.login, "header": header, "prefs": prefs, **context}
    )


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class PhaseIn(BaseModel):
    phase_type: str
    name: str = Field(min_length=1, max_length=60)
    start_date: datetime.date
    end_date: datetime.date
    goal: str | None = None


class AnnotationIn(BaseModel):
    kind: str
    label: str = Field(min_length=1, max_length=80)
    start_date: datetime.date
    end_date: datetime.date | None = None


class RaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    race_date: datetime.date
    distance: Literal["full", "half", "olympic", "sprint", "run", "other"]
    priority: Literal["A", "B", "C"]
    notes: str | None = None


class NoteIn(BaseModel):
    category: str
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1)
    valid_from: datetime.date | None = None
    valid_to: datetime.date | None = None
    rules: dict[str, Any] | None = None

    @field_validator("category")
    @classmethod
    def _category(cls, v: str) -> str:
        if v not in NOTE_CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(NOTE_CATEGORIES)}")
        return v

    @field_validator("rules")
    @classmethod
    def _rules(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v and not set(v) <= RULE_KEYS:
            raise ValueError(f"unknown rule keys: {', '.join(sorted(set(v) - RULE_KEYS))}")
        for plan in (v or {}).get("planned_labs") or []:
            if not isinstance(plan, dict) or not plan.get("markers"):
                raise ValueError(
                    'planned_labs entries need "markers" (catalogue keys) and optionally "due", "after", "title"'
                )
            for key in ("due", "after"):
                if plan.get(key):
                    datetime.date.fromisoformat(plan[key])
        return v or None


class FeedbackIn(BaseModel):
    rpe: int | None = Field(default=None, ge=1, le=10)
    feel: int | None = Field(default=None, ge=1, le=5)
    note: str | None = Field(default=None, max_length=4000)


class CompareIn(BaseModel):
    period1_start: datetime.date
    period1_end: datetime.date
    period2_start: datetime.date
    period2_end: datetime.date
    sport_type: str = "all"


def _season_error(exc: season_ops.SeasonError) -> HTTPException:
    return HTTPException(404 if "not found" in str(exc) else 400, detail=str(exc))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def today_page(request: Request, db: Database = Depends(get_db), config=Depends(get_config)) -> HTMLResponse:
    return page(request, "today.html", {"d": data.build_dashboard(db, config), "nav": "today"})


@router.get("/fitness", response_class=HTMLResponse, include_in_schema=False)
def fitness_page(request: Request, db: Database = Depends(get_db), config=Depends(get_config)) -> HTMLResponse:
    today = data.local_today(config)
    return page(request, "fitness.html", {"markers": data.markers(db, today), "today": today, "nav": "fitness"})


@router.get("/season", response_class=HTMLResponse, include_in_schema=False)
def season_page(request: Request, db: Database = Depends(get_db), config=Depends(get_config)) -> HTMLResponse:
    today = data.local_today(config)
    t = state.get_thresholds(db)
    return page(
        request,
        "season.html",
        {
            "s": data.season(db, today),
            "ctx": data.season_context(db, today, t),
            "nav": "season",
            "phase_types": season_ops.PHASE_TYPES,
            "annotation_kinds": season_ops.ANNOTATION_KINDS,
            "race_distances": season_ops.RACE_DISTANCES,
        },
    )


@router.get("/sessions", response_class=HTMLResponse, include_in_schema=False)
def sessions_page(
    request: Request,
    sport: str | None = None,
    page_no: int = 1,
    db: Database = Depends(get_db),
) -> HTMLResponse:
    limit = 50
    listing = data.sessions_list(db, sport or None, None, None, limit, (max(page_no, 1) - 1) * limit)
    return page(
        request, "sessions.html", {"l": listing, "sport": sport or "", "page_no": max(page_no, 1), "nav": "sessions"}
    )


@router.get("/sessions/{activity_id}", response_class=HTMLResponse, include_in_schema=False)
def session_page(
    request: Request, activity_id: str, v: int | None = None, compare: str | None = None, db: Database = Depends(get_db)
) -> HTMLResponse:
    detail = data.session_detail(db, activity_id)
    if detail is None:
        raise HTTPException(404, detail="session not found")
    detail["compare"] = data.session_compare(db, activity_id, compare)
    if v:
        from hart.server.grading import latest_grade

        detail["grade"] = latest_grade(db, activity_id, v) or detail["grade"]
    return page(request, "session.html", {"s": detail, "nav": "sessions"})


@router.get("/strength", response_class=HTMLResponse, include_in_schema=False)
def strength_page(
    request: Request, ex: str | None = None, db: Database = Depends(get_db), config=Depends(get_config)
) -> HTMLResponse:
    from hart.analytics.strength_progress import progression, weekly_sessions
    from hart.server.suggestions import _merged_rule, active_notes

    today = data.local_today(config)
    prog = progression(db, today)
    selected = next(
        (e for e in prog["exercises"] if e["key"] == ex), prog["exercises"][0] if prog["exercises"] else None
    )
    target = _merged_rule(active_notes(db, today), "min_sessions_per_week").get("strength")
    from hart.analytics.strength_progress import exercise_label, load_aliases

    aliases = [
        {"from": k, "to": v, "from_label": exercise_label(k), "to_label": exercise_label(v)}
        for k, v in sorted(load_aliases(db).items())
    ]
    return page(
        request,
        "strength.html",
        {
            "nav": "strength",
            "p": prog,
            "selected": selected,
            "weeks": weekly_sessions(db, today),
            "target": target,
            "aliases": aliases,
        },
    )


class AliasIn(BaseModel):
    name: str = Field(min_length=1, max_length=80, description="Exercise key to merge (e.g. SQUAT)")
    same_as: str | None = Field(default=None, max_length=80, description="Key it's the same lift as; null to undo")


@router.post("/api/strength/aliases")
def api_strength_alias(body: AliasIn, db: Database = Depends(get_db)) -> dict[str, Any]:
    from hart.analytics.strength_progress import ALIASES_KEY, load_aliases

    aliases = load_aliases(db)
    if body.same_as in (None, "", body.name):
        aliases.pop(body.name, None)
    else:
        # Refuse cycles (A → B → A): the target must not already point back.
        target, seen = body.same_as, set()
        while target in aliases and target not in seen:
            seen.add(target)
            target = aliases[target]
        if target == body.name:
            raise HTTPException(400, detail="that would merge the two lifts into each other")
        aliases[body.name] = body.same_as
    state.set_setting(db, ALIASES_KEY, aliases)
    return {"aliases": aliases}


@router.get("/api/strength")
def api_strength(db: Database = Depends(get_db), config=Depends(get_config)) -> dict[str, Any]:
    from hart.analytics.strength_progress import progression

    return data.clean(progression(db, data.local_today(config)))


@router.get("/notes", response_class=HTMLResponse, include_in_schema=False)
def notes_page(request: Request, db: Database = Depends(get_db)) -> HTMLResponse:
    notes = list_notes(db)
    return page(
        request,
        "notes.html",
        {
            "notes": notes,
            "categories": NOTE_CATEGORIES,
            "nav": "notes",
        },
    )


# ---------------------------------------------------------------------------
# Dashboard, readiness
# ---------------------------------------------------------------------------


@router.get("/api/dashboard")
def api_dashboard(db: Database = Depends(get_db), config=Depends(get_config)) -> dict[str, Any]:
    return data.build_dashboard(db, config)


@router.get("/api/readiness")
def api_readiness(
    date: datetime.date | None = None, db: Database = Depends(get_db), config=Depends(get_config)
) -> dict[str, Any]:
    return data.readiness_on(db, date or data.local_today(config), state.get_thresholds(db))


@router.post("/api/anomalies/{anomaly_id}/ack")
def api_ack_anomaly(anomaly_id: int, db: Database = Depends(get_db)) -> dict[str, Any]:
    if db.fetchone("SELECT 1 FROM anomaly_log WHERE id = ?", [anomaly_id]) is None:
        raise HTTPException(404, detail="anomaly not found")
    db.execute("UPDATE anomaly_log SET acknowledged = TRUE WHERE id = ?", [anomaly_id])
    return {"id": anomaly_id, "acknowledged": True}


# ---------------------------------------------------------------------------
# Fitness
# ---------------------------------------------------------------------------


@router.get("/api/fitness/pmc")
def api_pmc(
    range: RangeKey = "180d",
    sport: SportFilter = "combined",
    db: Database = Depends(get_db),
    config=Depends(get_config),
) -> dict[str, Any]:
    return data.pmc(db, data.local_today(config), range, sport)


@router.get("/api/fitness/volume")
def api_volume(weeks: int = 12, db: Database = Depends(get_db), config=Depends(get_config)) -> dict[str, Any]:
    return data.weekly_volume(db, data.local_today(config), min(max(weeks, 4), 104))


@router.get("/api/fitness/efficiency")
def api_efficiency(
    range: RangeKey = "1y", db: Database = Depends(get_db), config=Depends(get_config)
) -> dict[str, Any]:
    return data.efficiency(db, data.local_today(config), range)


@router.get("/api/fitness/health")
def api_health(range: RangeKey = "180d", db: Database = Depends(get_db), config=Depends(get_config)) -> dict[str, Any]:
    return data.health_series(db, data.local_today(config), range)


@router.get("/api/fitness/power-curve")
def api_power_curve(db: Database = Depends(get_db), config=Depends(get_config)) -> dict[str, Any]:
    return data.power_curves(db, data.local_today(config))


@router.get("/api/fitness/markers")
def api_markers(db: Database = Depends(get_db), config=Depends(get_config)) -> list[dict[str, Any]]:
    return data.markers(db, data.local_today(config))


@router.post("/api/fitness/compare")
def api_compare(body: CompareIn) -> Any:
    from hart import mcp_server

    return data.clean(
        json.loads(
            mcp_server.compare_periods(
                body.period1_start.isoformat(),
                body.period1_end.isoformat(),
                body.period2_start.isoformat(),
                body.period2_end.isoformat(),
                body.sport_type,
            )
        )
    )


# ---------------------------------------------------------------------------
# Season: phases, races, annotations
# ---------------------------------------------------------------------------


@router.get("/api/season")
def api_season(db: Database = Depends(get_db), config=Depends(get_config)) -> dict[str, Any]:
    today = data.local_today(config)
    return {**data.season(db, today), "context": data.season_context(db, today, state.get_thresholds(db))}


@router.post("/api/phases")
def api_create_phase(body: PhaseIn, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        return {"id": season_ops.create_phase(db, body.model_dump())}
    except season_ops.SeasonError as exc:
        raise _season_error(exc) from exc


@router.put("/api/phases/{phase_id}")
def api_update_phase(phase_id: int, body: PhaseIn, db: Database = Depends(get_db)) -> dict[str, Any]:
    if db.fetchone("SELECT 1 FROM training_phases WHERE id = ?", [phase_id]) is None:
        raise HTTPException(404, detail="phase not found")
    try:
        season_ops.update_phase(db, phase_id, body.model_dump())
    except season_ops.SeasonError as exc:
        raise _season_error(exc) from exc
    return {"id": phase_id}


@router.delete("/api/phases/{phase_id}")
def api_delete_phase(phase_id: int, db: Database = Depends(get_db)) -> dict[str, Any]:
    db.execute("DELETE FROM training_phases WHERE id = ?", [phase_id])
    return {"deleted": phase_id}


@router.post("/api/phases/confirm")
def api_confirm_phases(db: Database = Depends(get_db)) -> dict[str, Any]:
    n = db.fetchone("SELECT count(*) FROM training_phases WHERE NOT confirmed")[0]
    db.execute("UPDATE training_phases SET confirmed = TRUE WHERE NOT confirmed")
    return {"confirmed": n}


@router.post("/api/phases/generate")
def api_generate_phases(db: Database = Depends(get_db), config=Depends(get_config)) -> dict[str, Any]:
    try:
        return season_ops.regenerate_phases(db, data.local_today(config))
    except season_ops.SeasonError as exc:
        raise _season_error(exc) from exc


@router.post("/api/season/proposals/{proposal_id}/apply")
def api_apply_proposal(proposal_id: int, request: Request, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        out = season_ops.apply_proposal(db, proposal_id)
    except season_ops.SeasonError as exc:
        raise _season_error(exc) from exc
    if out.get("kind") in ("plan", season_ops.PLAN_IMPORT):
        from hart.server.plan_routes import refresh_suggestions

        removed = out.pop("garmin_workouts_removed", None) or []
        if out.get("garmin_workout_removed"):
            removed.append(out["garmin_workout_removed"])
        for workout_id in removed:
            request.app.state.runner.enqueue(
                "garmin_workout",
                {"delete_workout_id": workout_id},
                trigger="manual",
                dedupe_key=f"garmin_delete:{workout_id}",
            )
        dates = [datetime.date.fromisoformat(str(d)[:10]) for d in out.pop("plan_dates", [])]
        out["suggestions_refreshed"] = refresh_suggestions(request, db, dates)
    return data.clean(out)


@router.post("/api/season/proposals/{proposal_id}/dismiss")
def api_dismiss_proposal(proposal_id: int, db: Database = Depends(get_db)) -> dict[str, Any]:
    return season_ops.dismiss_proposal(db, proposal_id)


@router.post("/api/annotations")
def api_create_annotation(body: AnnotationIn, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        season_ops.validate_annotation(body.model_dump())
    except season_ops.SeasonError as exc:
        raise _season_error(exc) from exc
    new_id = db.fetchone(
        "INSERT INTO annotations (kind, label, start_date, end_date, source) VALUES (?, ?, ?, ?, 'manual') RETURNING id",
        [body.kind, body.label, body.start_date, body.end_date],
    )[0]
    return {"id": new_id}


@router.put("/api/annotations/{annotation_id}")
def api_update_annotation(annotation_id: int, body: AnnotationIn, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        season_ops.validate_annotation(body.model_dump())
    except season_ops.SeasonError as exc:
        raise _season_error(exc) from exc
    if db.fetchone("SELECT 1 FROM annotations WHERE id = ?", [annotation_id]) is None:
        raise HTTPException(404, detail="annotation not found")
    db.execute(
        "UPDATE annotations SET kind = ?, label = ?, start_date = ?, end_date = ?, source = 'manual' WHERE id = ?",
        [body.kind, body.label, body.start_date, body.end_date, annotation_id],
    )
    return {"id": annotation_id}


@router.delete("/api/annotations/{annotation_id}")
def api_delete_annotation(annotation_id: int, db: Database = Depends(get_db)) -> dict[str, Any]:
    if db.fetchone("SELECT 1 FROM annotations WHERE id = ?", [annotation_id]) is None:
        raise HTTPException(404, detail="annotation not found")
    db.execute("DELETE FROM annotations WHERE id = ?", [annotation_id])
    return {"deleted": annotation_id}


@router.post("/api/races")
def api_create_race(body: RaceIn, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        return season_ops.save_race(db, body.model_dump())
    except season_ops.SeasonError as exc:
        raise _season_error(exc) from exc


@router.put("/api/races/{race_id}")
def api_update_race(race_id: int, body: RaceIn, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        return season_ops.save_race(db, body.model_dump(), race_id)
    except season_ops.SeasonError as exc:
        raise _season_error(exc) from exc


@router.delete("/api/races/{race_id}")
def api_delete_race(race_id: int, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        return season_ops.delete_race(db, race_id)
    except season_ops.SeasonError as exc:
        raise _season_error(exc) from exc


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@router.get("/api/sessions")
def api_sessions(
    sport: str | None = None,
    start: datetime.date | None = Query(default=None, alias="from"),
    end: datetime.date | None = Query(default=None, alias="to"),
    limit: int = 50,
    offset: int = 0,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    return data.sessions_list(db, sport, start, end, min(limit, 200), max(offset, 0))


@router.get("/api/sessions/{activity_id}")
def api_session(activity_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    detail = data.session_detail(db, activity_id)
    if detail is None:
        raise HTTPException(404, detail="session not found")
    return detail


@router.get("/api/sessions/{activity_id}/streams")
def api_session_streams(activity_id: str, max_points: int = 1000, db: Database = Depends(get_db)) -> dict[str, Any]:
    return data.session_streams(db, activity_id, min(max(max_points, 50), 3000))


class GradeRequest(BaseModel):
    force: bool = False  # grade even sessions normally skipped (walks, very short…)


class BackfillRequest(BaseModel):
    days: int = Field(default=14, ge=1, le=365)


def _enqueue_grade(request: Request, activity_id: str, trigger: str, force: bool = False) -> dict[str, Any]:
    return request.app.state.runner.enqueue(
        "grade",
        {"activity_id": activity_id, "trigger": trigger, "force": force},
        trigger="manual",
        dedupe_key=f"grade:{activity_id}",
    )


@router.get("/api/sessions/{activity_id}/grade")
def api_session_grade(activity_id: str, version: int | None = None, db: Database = Depends(get_db)) -> dict[str, Any]:
    from hart.server.grading import latest_grade

    grade = latest_grade(db, activity_id, version)
    if grade is None:
        raise HTTPException(404, detail="not graded yet")
    return grade


@router.get("/api/sessions/{activity_id}/compare")
def api_session_compare(activity_id: str, other: str | None = None, db: Database = Depends(get_db)) -> dict[str, Any]:
    out = data.session_compare(db, activity_id, other)
    if out is None:
        raise HTTPException(404, detail="session not found")
    return data.clean(out)


@router.get("/api/sessions/{activity_id}/features")
def api_session_features(activity_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    """The facts a grade would be based on right now (no Claude call)."""
    from hart.analytics.grading_features import build_features

    features = build_features(db, activity_id, state.get_thresholds(db))
    if features is None:
        raise HTTPException(404, detail="session not found")
    return data.clean(features)


@router.post("/api/sessions/{activity_id}/grade")
def api_regrade(
    activity_id: str, body: GradeRequest, request: Request, db: Database = Depends(get_db)
) -> dict[str, Any]:
    if db.fetchone("SELECT 1 FROM activities WHERE activity_id = ?", [activity_id]) is None:
        raise HTTPException(404, detail="session not found")
    return _enqueue_grade(request, activity_id, "manual", body.force)


@router.post("/api/grades/backfill")
def api_grade_backfill(body: BackfillRequest, request: Request, db: Database = Depends(get_db)) -> dict[str, Any]:
    """Queue grading for sessions in the last N days that have no grade yet."""
    ids = [
        r[0]
        for r in db.fetchall(
            "SELECT a.activity_id FROM activities a WHERE a.start_time >= current_date - ? * INTERVAL 1 DAY "
            "AND NOT EXISTS (SELECT 1 FROM session_grades g WHERE g.activity_id = a.activity_id "
            "AND g.status IN ('graded', 'ungraded')) ORDER BY a.start_time",
            [body.days],
        )
    ]
    jobs = [_enqueue_grade(request, i, "backfill") for i in ids]
    return {"queued": sum(1 for j in jobs if j["status"] == "queued"), "activities": len(ids)}


class GarminNoteIn(BaseModel):
    action: Literal["use_garmin", "keep_mine"]


@router.post("/api/sessions/{activity_id}/note/garmin")
def api_session_note_garmin(
    activity_id: str, body: GarminNoteIn, request: Request, db: Database = Depends(get_db)
) -> dict[str, Any]:
    """Garmin's description changed after the note was written in hart: take it, or keep the hart note."""
    if db.fetchone("SELECT 1 FROM session_feedback WHERE activity_id = ?", [activity_id]) is None:
        raise HTTPException(404, detail="no note for this session")
    if body.action == "use_garmin":
        db.execute(
            "UPDATE session_feedback SET comment = NULL, note_garmin_seen = NULL WHERE activity_id = ?", [activity_id]
        )
        graded = db.fetchone(
            "SELECT count(*) FROM session_grades WHERE activity_id = ? AND status = 'graded'", [activity_id]
        )[0]
        return {"note": "garmin", "regrade": _enqueue_grade(request, activity_id, "note_edited") if graded else None}
    db.execute(
        "UPDATE session_feedback AS f SET note_garmin_seen = a.description FROM activities AS a "
        "WHERE a.activity_id = f.activity_id AND f.activity_id = ?",
        [activity_id],
    )
    return {"note": "mine"}


@router.post("/api/sessions/{activity_id}/feedback")
def api_session_feedback(
    activity_id: str, body: FeedbackIn, request: Request, db: Database = Depends(get_db)
) -> dict[str, Any]:
    if db.fetchone("SELECT 1 FROM activities WHERE activity_id = ?", [activity_id]) is None:
        raise HTTPException(404, detail="session not found")
    garmin = db.fetchone("SELECT description FROM activities WHERE activity_id = ?", [activity_id])[0] or ""
    garmin = garmin.replace("\r\n", "\n").strip()
    current = db.fetchone("SELECT comment, note_garmin_seen FROM session_feedback WHERE activity_id = ?", [activity_id])
    note = (body.note or "").replace("\r\n", "\n").strip()
    if current is None or current[0] is None:
        # Following Garmin: an unchanged (or empty, with nothing in Garmin) note keeps following it.
        comment, seen = (None, None) if note == garmin else (note, garmin or None)
    else:
        comment, seen = note, current[1]
    db.execute(
        "INSERT OR REPLACE INTO session_feedback (activity_id, rpe, feel, comment, note_garmin_seen, updated_at) "
        "VALUES (?, ?, ?, ?, ?, current_timestamp)",
        [activity_id, body.rpe, body.feel, comment, seen],
    )
    # Feedback after grading → one regrade with it, never more.
    regrade = None
    graded = db.fetchone(
        "SELECT count(*) FROM session_grades WHERE activity_id = ? AND status = 'graded'", [activity_id]
    )[0]
    already = db.fetchone(
        "SELECT count(*) FROM session_grades WHERE activity_id = ? "
        "AND json_extract_string(features, '$.trigger') = 'feedback'",
        [activity_id],
    )[0]
    if graded and not already:
        regrade = _enqueue_grade(request, activity_id, "feedback")
    return {"activity_id": activity_id, "saved": True, "regrade": regrade}


# ---------------------------------------------------------------------------
# Athlete notes
# ---------------------------------------------------------------------------


def list_notes(db: Database, status: str | None = None) -> list[dict[str, Any]]:
    where = "WHERE status = ?" if status else ""
    notes = data.rows(
        db,
        "SELECT id, category, title, body, valid_from, valid_to, rules, status, source, created_at, updated_at, "
        f"target_id, proposed_action, proposal_reason FROM athlete_notes {where} "
        "ORDER BY CASE status WHEN 'proposed' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, category, id",
        [status] if status else [],
    )
    for n in notes:
        if isinstance(n["rules"], str):
            n["rules"] = json.loads(n["rules"])
    by_id = {n["id"]: n for n in notes}
    for n in notes:  # a proposed change shows the note it would change
        target = by_id.get(n["target_id"]) if n.get("target_id") else None
        n["target"] = (
            {k: target[k] for k in ("id", "title", "category", "body", "valid_from", "valid_to")} if target else None
        )
    return notes


@router.get("/api/notes")
def api_notes(status: str | None = None, db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    return list_notes(db, status)


@router.post("/api/notes")
def api_create_note(body: NoteIn, request: Request, db: Database = Depends(get_db)) -> dict[str, Any]:
    new_id = db.fetchone(
        "INSERT INTO athlete_notes (category, title, body, valid_from, valid_to, rules, status, source) "
        "VALUES (?, ?, ?, ?, ?, ?, 'active', 'manual') RETURNING id",
        [
            body.category,
            body.title,
            body.body,
            body.valid_from,
            body.valid_to,
            json.dumps(body.rules) if body.rules else None,
        ],
    )[0]
    _resync_health(db)
    return {"id": new_id}


@router.put("/api/notes/{note_id}")
def api_update_note(note_id: int, body: NoteIn, db: Database = Depends(get_db)) -> dict[str, Any]:
    if db.fetchone("SELECT 1 FROM athlete_notes WHERE id = ?", [note_id]) is None:
        raise HTTPException(404, detail="note not found")
    # Edited notes become manual, so seed-file refreshes never overwrite them.
    db.execute(
        "UPDATE athlete_notes SET category = ?, title = ?, body = ?, valid_from = ?, valid_to = ?, rules = ?, "
        "source = 'manual', updated_at = current_timestamp WHERE id = ?",
        [
            body.category,
            body.title,
            body.body,
            body.valid_from,
            body.valid_to,
            json.dumps(body.rules) if body.rules else None,
            note_id,
        ],
    )
    _resync_health(db)
    return {"id": note_id}


def _resync_health(db: Database) -> None:
    """Notes drive some health reminders (physio check-ins, planned re-checks)."""
    from hart.server.health import sync_checks

    sync_checks(db, datetime.date.today())


def _set_note_status(db: Database, note_id: int, status: str) -> dict[str, Any]:
    if db.fetchone("SELECT 1 FROM athlete_notes WHERE id = ?", [note_id]) is None:
        raise HTTPException(404, detail="note not found")
    db.execute("UPDATE athlete_notes SET status = ?, updated_at = current_timestamp WHERE id = ?", [status, note_id])
    _resync_health(db)
    return {"id": note_id, "status": status}


def _change_proposal(db: Database, note_id: int) -> tuple[int, str] | None:
    row = db.fetchone(
        "SELECT target_id, proposed_action FROM athlete_notes WHERE id = ? AND status = 'proposed' "
        "AND proposed_action IN ('update', 'archive')",
        [note_id],
    )
    return (row[0], row[1]) if row else None


@router.post("/api/notes/{note_id}/approve")
def api_approve_note(note_id: int, db: Database = Depends(get_db)) -> dict[str, Any]:
    """Approve a proposal: a new note becomes active; a proposed change is applied to its note."""
    change = _change_proposal(db, note_id)
    if change is None:
        return _set_note_status(db, note_id, "active")
    target_id, action = change
    if db.fetchone("SELECT 1 FROM athlete_notes WHERE id = ?", [target_id]) is None:
        db.execute("DELETE FROM athlete_notes WHERE id = ?", [note_id])
        raise HTTPException(404, detail="the note this change was for no longer exists")
    if action == "archive":
        db.execute(
            "UPDATE athlete_notes SET status = 'archived', updated_at = current_timestamp WHERE id = ?", [target_id]
        )
    else:
        # The change lands on the original note (it keeps its id and history); edited notes become manual,
        # so seed-file refreshes never overwrite them.
        db.execute(
            "UPDATE athlete_notes AS t SET category = p.category, title = p.title, body = p.body, "
            "valid_from = p.valid_from, valid_to = p.valid_to, source = 'manual', updated_at = current_timestamp "
            "FROM athlete_notes AS p WHERE p.id = ? AND t.id = ?",
            [note_id, target_id],
        )
    db.execute("DELETE FROM athlete_notes WHERE id = ?", [note_id])
    _resync_health(db)
    return {"id": target_id, "status": "archived" if action == "archive" else "active", "applied": action}


@router.post("/api/notes/{note_id}/archive")
def api_archive_note(note_id: int, db: Database = Depends(get_db)) -> dict[str, Any]:
    if _change_proposal(db, note_id) is not None:  # dismissing a proposed change just drops it
        db.execute("DELETE FROM athlete_notes WHERE id = ?", [note_id])
        return {"id": note_id, "status": "dismissed"}
    return _set_note_status(db, note_id, "archived")


@router.post("/api/notes/{note_id}/restore")
def api_restore_note(note_id: int, db: Database = Depends(get_db)) -> dict[str, Any]:
    return _set_note_status(db, note_id, "active")
