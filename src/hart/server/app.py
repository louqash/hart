"""hart server: FastAPI application.

One process owns the DuckDB file.  It serves the web UI and JSON API, the
MCP endpoint used by every Claude (``/mcp``), and runs the job queue and
scheduler.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import os
import re
import secrets
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from hart.config import HartSettings, get_config
from hart.server import chat_routes, evening, health_routes, plan_routes, routes, settings, settings_routes, state
from hart.server.chat import ChatService
from hart.server.claude.runner import ClaudeRunner
from hart.server.auth import AuthMiddleware
from hart.server.data import claude_status, claude_usage
from hart.server.jobs.handlers import make_handlers
from hart.server.jobs.pipeline import has_sleep_for, row_dict
from hart.server.jobs.runner import JobRunner
from hart.server.jobs.scheduler import Scheduler, garmin_blocked, last_successful_sync
from hart.server.seed import seed_all
from hart.storage.database import Database

logger = logging.getLogger(__name__)

WEB_DIR = routes.WEB_DIR
templates = routes.templates  # shared env with the page filters

TOKEN_FILE_RE = re.compile(r"^[A-Za-z0-9_.-]+\.json$")
MAX_TOKEN_FILE_BYTES = 64 * 1024


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_db(request: Request) -> Iterator[Database]:
    """A cursor-backed database view per request."""
    cur = request.app.state.db.cursor()
    try:
        yield cur
    finally:
        cur.close()


def _runner(request: Request) -> JobRunner:
    return request.app.state.runner


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class SyncRequest(BaseModel):
    full: bool = True


class Vo2maxBackfillRequest(BaseModel):
    start: datetime.date | None = None
    end: datetime.date | None = None


class DecouplingBackfillRequest(BaseModel):
    only_missing: bool = False


class GarminTokensRequest(BaseModel):
    files: dict[str, str] = Field(description="Token file name -> JSON content, as written by `hart auth`.")


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------


def _local_today(config: HartSettings) -> datetime.date:
    return datetime.datetime.now(ZoneInfo(config.server.tz)).date()


def build_today(db: Database, config: HartSettings) -> dict[str, Any]:
    """Minimal dashboard bundle for the CLI (`hart status`)."""
    today = _local_today(config)
    race = row_dict(
        db,
        "SELECT name, race_date FROM races WHERE priority = 'A' AND race_date >= ? "
        "ORDER BY race_date LIMIT 1",
        [today],
    )
    load = row_dict(
        db,
        "SELECT date, ctl, atl, tsb FROM daily_training_load "
        "WHERE sport_type = 'combined' AND date <= ? ORDER BY date DESC LIMIT 1",
        [today],
    )
    recovery = row_dict(
        db,
        "SELECT date, recovery_score FROM daily_recovery WHERE date <= ? ORDER BY date DESC LIMIT 1",
        [today],
    )
    last_sync = last_successful_sync(db)
    return {
        "date": today,
        "race": None if race is None else {
            "name": race["name"],
            "date": race["race_date"],
            "days_to_race": (race["race_date"] - today).days,
        },
        "load": load,
        "recovery": recovery,
        "sleep_synced_today": has_sleep_for(db, today),
        "last_successful_sync": last_sync,
    }


def job_details(job: dict[str, Any]) -> str:
    """One-line human summary of a job for the jobs table."""
    result = job.get("result") or {}
    if job["status"] in ("queued", "running"):
        return ""
    parts: list[str] = []
    if job["type"] in ("sync", "sync_light") and isinstance(result, dict) and "steps" in result:
        steps = result["steps"]

        def out(name: str) -> dict[str, Any]:
            value = (steps.get(name) or {}).get("out")
            return value if isinstance(value, dict) else {}

        health = out("garmin_health")
        if health.get("from"):
            start = datetime.date.fromisoformat(health["from"])
            end = datetime.date.fromisoformat(health["to"])
            span = end.strftime("%d %b") if start == end else f"{start:%d}–{end:%d %b}"
            parts.append(f"health refreshed for {span}")
        elif "days" in health:
            parts.append(f"health refreshed for {health['days']} days")
        if "new" in out("garmin_activities"):
            new = out("garmin_activities")["new"]
            parts.append(f"{new} new activit{'y' if new == 1 else 'ies'}" if new else "no new activities")
            renamed = out("garmin_activities").get("renamed")
            if renamed:
                parts.append(f"{renamed} renamed")
        if out("strength_edits").get("updated"):
            n = out("strength_edits")["updated"]
            parts.append(f"gym sets updated for {n} session{'s' if n != 1 else ''}")
        if out("effort_edits").get("updated"):
            n = out("effort_edits")["updated"]
            parts.append(f"RPE/feel updated for {n} session{'s' if n != 1 else ''}")
        if "written" in out("recovery_backfill"):
            parts.append(f"recovery backfilled for {out('recovery_backfill')['written']} days")
        elif "written" in out("recovery"):
            parts.append(f"recovery recalculated for {out('recovery')['written']} days")
        if "inserted" in out("anomalies"):
            n = out("anomalies")["inserted"]
            parts.append(f"{n} new anomal{'y' if n == 1 else 'ies'}" if n else "no new anomalies")
        if job["type"] == "sync_light":
            parts.append("sleep synced" if result.get("sleep_today") else "no sleep yet")
        errors = result.get("errors") or []
        if errors:
            parts.append(f"{len(errors)} error(s): {errors[0]}")
    elif job["type"] == "vo2max_backfill" and "updated" in result:
        parts.append(f"VO2max set on {result['updated']} days ({result['from']} → {result['to']})")
        if result.get("no_health_row"):
            parts.append(f"{result['no_health_row']} estimate(s) skipped (no health row)")
    elif job["type"] == "decoupling_backfill" and "computed" in result:
        parts.append(f"decoupling computed for {result['computed']} of {result['checked']} bike/run sessions")
        if result.get("errors"):
            parts.append(f"{result['errors']} error(s)")
    elif job["type"] == "backup" and "bytes" in result:
        parts.append(f"{result['bytes'] / 1_048_576:.0f} MB → {result['path']}")
        if result.get("pruned"):
            parts.append(f"pruned {len(result['pruned'])}")
    if job.get("error"):
        parts.insert(0, job["error"])
    return " · ".join(parts)


def _last_job(db: Database, job_type: str) -> dict[str, Any] | None:
    return row_dict(
        db,
        "SELECT id, status, error, finished_at FROM jobs WHERE type = ? AND status IN ('ok', 'error') "
        "ORDER BY id DESC LIMIT 1",
        [job_type],
    )


def build_system(db: Database, config: HartSettings, runner: JobRunner) -> dict[str, Any]:
    now = datetime.datetime.now(tz=datetime.timezone.utc)

    def count(sql: str) -> int:
        return int(db.fetchone(sql)[0])

    sync_state = [
        {"source": r[0], "last_sync_at": r[1]}
        for r in db.fetchall("SELECT source, last_sync_at FROM sync_state ORDER BY source")
    ]
    db_path = Path(config.db_path)
    return {
        "server": {"env": config.server.env, "tz": config.server.tz},
        "database": {
            "path": str(db_path),
            "size_bytes": db_path.stat().st_size if db_path.is_file() else None,
            "schema_version": db.fetchone("SELECT max(version) FROM schema_version")[0],
        },
        "backup": _last_job(db, "backup"),
        "sync": {
            "last_successful_sync": last_successful_sync(db),
            "blocked": garmin_blocked(db, now),
            "backoff_until": state.get_setting(db, state.GARMIN_BACKOFF_UNTIL),
            "sources": sync_state,
        },
        "counts": {
            "activities": count("SELECT count(*) FROM activities"),
            "recovery_days": count("SELECT count(*) FROM daily_recovery"),
            "unacknowledged_anomalies": count("SELECT count(*) FROM anomaly_log WHERE NOT acknowledged"),
            "proposed_notes": count("SELECT count(*) FROM athlete_notes WHERE status = 'proposed'"),
            "annotations": count("SELECT count(*) FROM annotations"),
        },
        "jobs": [{**j, "details": job_details(j)} for j in runner.list_jobs(limit=30)],
        "claude": claude_status(db, config, _local_today(config)),
        "claude_usage": claude_usage(db, _local_today(config)),
        "evening": {
            "configured": evening.configured(config),
            "enabled": bool(settings.get(db, "evening_message_enabled")),
            "time": settings.get(db, "evening_message_time"),
            "last_sent": state.get_setting(db, evening.SENT_KEY),
            "last_failed": state.get_setting(db, evening.FAILED_KEY),
        },
    }


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    config: HartSettings | None = None,
    *,
    run_scheduler: bool = True,
    handlers: dict[str, Any] | None = None,
    claude_client_factory: Any = None,
) -> FastAPI:
    """Build the hart server app.

    ``run_scheduler``, ``handlers`` and ``claude_client_factory`` exist for
    tests (no time-based jobs, fake sync handlers, a scripted Claude).
    """
    from hart import mcp_server

    config = config or get_config()
    internal_token = secrets.token_urlsafe(32)

    # A fresh MCP session manager per app (its run() works once per instance).
    mcp_server.mcp._session_manager = None
    mcp_app = mcp_server.mcp.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(config.db_path).connect()
        with contextlib.closing(db.cursor()) as cur:
            try:
                # A `hart demo` database never reads seed files: they may hold real data.
                if not state.is_demo(cur):
                    seed_all(cur, config.server.seed_dir)
            except Exception:  # noqa: BLE001 — seeding must never block startup
                logger.exception("Seeding failed")
            # A restart is when a renewed Claude token arrives: don't keep background work paused
            # for an old auth failure or usage limit (a still-bad token just pauses it again).
            from hart.server.grading import CLAUDE_PAUSED_UNTIL

            state.delete_setting(cur, CLAUDE_PAUSED_UNTIL)

        runner_ref: dict[str, JobRunner] = {}
        runner = JobRunner(db, handlers or make_handlers(config, runner_ref))
        runner_ref["runner"] = runner
        mcp_server.configure_server(db, runner.enqueue, runner.get_job)

        app.state.db = db
        app.state.config = config
        app.state.runner = runner
        app.state.internal_token = internal_token
        claude_runner = ClaudeRunner(
            db,
            mcp_url=config.claude.mcp_url or f"http://127.0.0.1:{config.server.port}/mcp",
            internal_token=internal_token,
            workspace=config.claude.workspace,
            max_concurrency=config.claude.max_concurrency,
            client_factory=claude_client_factory,
        )
        app.state.claude = claude_runner
        runner_ref["claude"] = claude_runner
        app.state.chat = ChatService(db, claude_runner, config)

        await runner.start()
        scheduler = Scheduler(db, runner, config.server.tz, config)
        if run_scheduler:
            await scheduler.start()
        try:
            async with mcp_server.mcp.session_manager.run():
                yield
        finally:
            await scheduler.stop()
            await runner.stop()
            mcp_server.configure_server(None, None, None)  # type: ignore[arg-type]
            with contextlib.suppress(Exception):
                db.execute("CHECKPOINT")
            db.close()
            logger.info("hart server stopped cleanly")

    app = FastAPI(title="hart", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(
        AuthMiddleware,
        dev=config.server.env == "dev",
        internal_token=internal_token,
        public_host=config.server.public_host,
        identity_header=config.server.auth_header,
        allowed_users=config.server.allowed_users,
    )

    # ---- health ----

    @app.get("/healthz", include_in_schema=False)
    def healthz(request: Request) -> dict[str, Any]:
        runner: JobRunner | None = getattr(request.app.state, "runner", None)
        worker = getattr(runner, "_worker", None)
        return {
            "ok": True,
            "db_open": getattr(request.app.state, "db", None) is not None,
            "runner_alive": worker is not None and not worker.done(),
            # Deployment check: is Tailscale Serve passing the identity header?
            "tailscale_login": request.headers.get("tailscale-user-login"),
        }

    # ---- pages ----

    @app.get("/system", response_class=HTMLResponse, include_in_schema=False)
    def system_page(request: Request, db: Database = Depends(get_db)) -> HTMLResponse:
        return routes.page(request, "system.html", {
            "s": build_system(db, config, _runner(request)), "today": build_today(db, config), "nav": "system",
        })

    # ---- API ----

    @app.get("/api/today")
    def api_today(db: Database = Depends(get_db)) -> dict[str, Any]:
        return build_today(db, config)

    @app.get("/api/system")
    def api_system(request: Request, db: Database = Depends(get_db)) -> dict[str, Any]:
        return build_system(db, config, _runner(request))

    @app.post("/api/sync")
    def api_sync(body: SyncRequest, request: Request) -> dict[str, Any]:
        # Manual syncs are allowed even while scheduled syncs are paused.
        job_type = "sync" if body.full else "sync_light"
        return _runner(request).enqueue(job_type, {"manual": True}, trigger="manual", dedupe_key=job_type)

    @app.post("/api/backfill/vo2max")
    def api_backfill_vo2max(body: Vo2maxBackfillRequest, request: Request) -> dict[str, Any]:
        payload = {k: v.isoformat() for k, v in (("start", body.start), ("end", body.end)) if v}
        return _runner(request).enqueue(
            "vo2max_backfill", payload, trigger="manual", dedupe_key="vo2max_backfill"
        )

    @app.post("/api/backup")
    def api_backup(request: Request) -> dict[str, Any]:
        return _runner(request).enqueue("backup", {}, trigger="manual", dedupe_key="backup")

    @app.post("/api/backfill/decoupling")
    def api_backfill_decoupling(body: DecouplingBackfillRequest, request: Request) -> dict[str, Any]:
        return _runner(request).enqueue(
            "decoupling_backfill", {"only_missing": body.only_missing},
            trigger="manual", dedupe_key="decoupling_backfill",
        )

    @app.get("/api/jobs")
    def api_jobs(request: Request, type: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return _runner(request).list_jobs(job_type=type, status=status, limit=min(limit, 200))

    class EveningIn(BaseModel):
        enabled: bool

    @app.put("/api/evening/enabled")
    def api_evening_enabled(body: EveningIn, db: Database = Depends(get_db)) -> dict[str, Any]:
        settings.set_value(db, "evening_message_enabled", body.enabled)
        return {"enabled": body.enabled}

    @app.post("/api/evening/test")
    def api_evening_test(request: Request) -> dict[str, Any]:
        if not evening.configured(config):
            raise HTTPException(400, detail="Discord isn't configured — set HART_DISCORD_WEBHOOK_URL")
        return _runner(request).enqueue("evening_message", {"test": True}, trigger="manual",
                                        dedupe_key="evening_message_test")

    @app.get("/api/jobs/active")
    def api_jobs_active(db: Database = Depends(get_db)) -> dict[str, Any]:
        """What's running now, for the page-wide activity bar (declared before /api/jobs/{job_id})."""
        active = [
            {"id": r[0], "type": r[1], "status": r[2]}
            for r in db.fetchall("SELECT id, type, status FROM jobs WHERE status IN ('queued', 'running') ORDER BY id")
        ]
        last = db.fetchone("SELECT max(id) FROM jobs WHERE status NOT IN ('queued', 'running')")[0]
        return {"active": active, "last_finished_id": last}

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: int, request: Request) -> dict[str, Any]:
        job = _runner(request).get_job(job_id)
        if job is None:
            raise HTTPException(404, detail="job not found")
        return job

    @app.post("/api/garmin/tokens")
    def api_garmin_tokens(body: GarminTokensRequest, request: Request, db: Database = Depends(get_db)) -> dict[str, Any]:
        """Receive Garmin OAuth tokens from `hart auth` on the laptop."""
        if not body.files:
            raise HTTPException(400, detail="no token files")
        for name, content in body.files.items():
            if not TOKEN_FILE_RE.match(name) or len(content.encode()) > MAX_TOKEN_FILE_BYTES:
                raise HTTPException(400, detail=f"rejected token file {name!r}")
        token_dir = Path(config.garmin_token_path)
        token_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(token_dir, 0o700)
        for name, content in body.files.items():
            path = token_dir / name
            path.write_text(content, encoding="utf-8")
            os.chmod(path, 0o600)
        state.delete_setting(db, state.GARMIN_AUTH_PAUSED)
        job = _runner(request).enqueue("sync", {"manual": True}, trigger="manual", dedupe_key="sync")
        return {"saved": sorted(body.files), "sync": job}

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": str(exc.status_code), "message": exc.detail}}, status_code=exc.status_code
        )

    app.include_router(routes.router)
    app.include_router(chat_routes.router)
    app.include_router(plan_routes.router)
    app.include_router(health_routes.router)
    app.include_router(settings_routes.router)

    # PWA: the service worker must be served from / to control
    # the whole site; it caches static assets only, never data.
    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        return FileResponse(WEB_DIR / "static" / "sw.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(WEB_DIR / "static" / "manifest.webmanifest", media_type="application/manifest+json")
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    # MCP: add its route directly (a Mount would redirect /mcp -> /mcp/).
    app.router.routes.extend(mcp_app.routes)
    return app
