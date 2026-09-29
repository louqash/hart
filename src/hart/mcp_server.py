"""MCP server exposing hart's training data and analytics as tools.

This is the core interface between Claude Code (Ember, agents) and hart.  It uses the ``mcp`` package with ``FastMCP`` to
expose all training data, analytics computations, sync operations, and
pre-aggregated context bundles over the stdio transport.

Start via::

    hart-mcp          # uses the project.scripts entry point
    python -m hart.mcp_server   # direct invocation
"""

from __future__ import annotations

import dataclasses
import datetime
import functools
import json
import logging
import threading
import traceback
from collections.abc import Callable
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastMCP application
# ---------------------------------------------------------------------------

# Stateless JSON transport: tools need no MCP session state, and clients
# survive server restarts.  DNS-rebinding/Origin checks are done by the
# hart server auth middleware, which knows the real public hostname.
mcp = FastMCP(
    "hart",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _tool(*tool_args: Any, **tool_kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a synchronous tool that runs in a worker thread.

    FastMCP calls synchronous tools directly on the event loop, so one slow
    query would stall the whole hart server (web UI included).  The module
    keeps the plain synchronous function.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def run_in_thread(*args: Any, **kwargs: Any) -> Any:
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

        mcp.tool(*tool_args, **tool_kwargs)(run_in_thread)
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Lazy initialisation helpers
# ---------------------------------------------------------------------------

_db_instance = None
_config_instance = None

# Set by hart server (configure_server): the server's database plus job hooks.
_server_db: Any = None
_enqueue_job: Callable[..., dict[str, Any]] | None = None
_lookup_job: Callable[[int], dict[str, Any] | None] | None = None
_thread_local = threading.local()


def configure_server(
    db: Any,
    enqueue_job: Callable[..., dict[str, Any]],
    lookup_job: Callable[[int], dict[str, Any] | None],
) -> None:
    """Run the tools inside hart server: share its database and job queue."""
    global _server_db, _enqueue_job, _lookup_job
    _server_db = db
    _enqueue_job = enqueue_job
    _lookup_job = lookup_job


def _get_config():
    """Return the cached HartSettings singleton."""
    global _config_instance
    if _config_instance is None:
        from hart.config import get_config

        _config_instance = get_config()
    return _config_instance


def _get_db():
    """Return a connected Database singleton, lazily initialised.

    Inside hart server each worker thread gets its own cursor on the server's
    database; the stdio server opens the file itself.
    """
    global _db_instance
    if _server_db is not None:
        cached = getattr(_thread_local, "db", None)
        # Keyed by the server database, so a re-configured server (tests) never gets a stale cursor.
        if cached is None or cached[0] is not _server_db:
            cached = (_server_db, _server_db.cursor())
            _thread_local.db = cached
        return cached[1]
    if _db_instance is None:
        from hart.storage.database import get_database

        config = _get_config()
        _db_instance = get_database(config.db_path)
    return _db_instance


# ---------------------------------------------------------------------------
# JSON serialisation helper
# ---------------------------------------------------------------------------


def _json(obj: Any) -> str:
    """Serialise *obj* to a pretty-printed JSON string.

    Handles datetime, date, Pydantic models, enums, numpy types, and
    other common non-JSON-native types.
    """

    def _default(o: Any) -> Any:
        if hasattr(o, "model_dump"):
            return o.model_dump()
        if isinstance(o, (datetime.datetime, datetime.date)):
            return o.isoformat()
        if isinstance(o, datetime.timedelta):
            return o.total_seconds()
        if hasattr(o, "value"):  # enum
            return o.value
        # numpy scalars
        type_name = type(o).__module__
        if type_name == "numpy":
            return o.item()
        raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")

    return json.dumps(obj, indent=2, default=_default, ensure_ascii=False)


def _parse_date(s: str) -> datetime.date | None:
    """Parse an ISO date string, returning None if empty or invalid."""
    if not s or not s.strip():
        return None
    try:
        return datetime.date.fromisoformat(s.strip())
    except (ValueError, TypeError):
        return None


def _default_start(days_back: int = 30) -> datetime.date:
    """Return a sensible default start date (N days ago)."""
    return datetime.date.today() - datetime.timedelta(days=days_back)


def _sync_meta() -> dict[str, Any]:
    """Return sync freshness metadata to embed in query tool responses."""
    try:
        from hart.storage.queries import get_sync_state

        db = _get_db()
        now = datetime.datetime.now()
        sources = ["garmin_health", "garmin_activities"]
        meta: dict[str, Any] = {}
        max_stale_hours = 0.0

        for source in sources:
            state = get_sync_state(db, source)
            if state and state.get("last_sync_at"):
                last_sync = state["last_sync_at"]
                if isinstance(last_sync, str):
                    last_sync = datetime.datetime.fromisoformat(last_sync)
                elif not isinstance(last_sync, datetime.datetime):
                    last_sync = datetime.datetime.fromisoformat(str(last_sync))
                hours = (now - last_sync).total_seconds() / 3600
                meta[source] = {
                    "last_sync": last_sync.isoformat(),
                    "hours_stale": round(hours, 1),
                }
                max_stale_hours = max(max_stale_hours, hours)
            else:
                meta[source] = {"status": "never_synced"}

        meta["suggest_sync"] = max_stale_hours > 12
        return meta
    except Exception:
        return {}


# =========================================================================
# MCP RESOURCES  (static / rarely-changing data — no tool call needed)
# =========================================================================

_AGENTS_DIR = __import__("pathlib").Path(__file__).resolve().parent.parent.parent / ".claude" / "agents"


@mcp.resource(
    "athlete://profile",
    name="athlete-profile",
    description="Athlete configuration: name, max HR, and analytics parameters.",
    mime_type="application/json",
)
def athlete_profile_resource() -> str:
    """Return the full athlete config as JSON."""
    from hart.server import settings

    config = _get_config()
    db = _get_db()

    return _json(
        {
            "athlete": {
                "name": settings.get(db, "athlete_name"),
                "power_single_sided": settings.get(db, "power_single_sided"),
                "has_coach": settings.get(db, "has_coach"),
            },
            "analytics": {
                "ctl_time_constant": config.analytics.ctl_time_constant,
                "atl_time_constant": config.analytics.atl_time_constant,
                "anomaly_z_threshold": config.analytics.anomaly_z_threshold,
                "recovery_weights": dataclasses.asdict(config.analytics.recovery_weights),
            },
        }
    )


@mcp.resource(
    "schema://tables",
    name="schema-tables",
    description=(
        "Database schema overview: all table names, their purpose, and column counts. "
        "Read before writing run_sql_query calls to know what tables exist."
    ),
    mime_type="application/json",
)
def schema_tables_resource() -> str:
    """Return table names, descriptions, and column counts from the live database."""
    db = _get_db()

    rows = db.fetchdf(
        "SELECT table_name, estimated_size FROM duckdb_tables() WHERE schema_name = 'main' ORDER BY table_name"
    ).to_dict(orient="records")  # type: ignore[union-attr]

    _TABLE_DESCRIPTIONS: dict[str, str] = {
        "activities": "One row per training session. Core activity data: sport, time, distance, HR, power, pace.",
        "strength_sets": "One row per set of a strength session: set_type (active/rest), reps, weight_kg, exercise category/name.",
        "activity_streams": "Second-by-second time-series for each activity: HR, power, cadence, speed, GPS, running form.",
        "activity_laps": "Lap-level summaries within activities.",
        "activity_metrics": "Computed metrics per activity: TSS, intensity factor, zone distributions, efficiency factor, aerobic decoupling.",
        "hrv_samples": "Raw R-R interval samples from activity HRV recording.",
        "daily_health": "Daily Garmin health aggregates: resting HR, stress, Body Battery, training readiness, VO2max, steps.",
        "sleep_records": "Nightly sleep: duration, stage breakdown (deep/light/REM), SpO2, sleep score, overnight HRV.",
        "hrv_daily": "Daily HRV: last-night value, 7-day rolling average, baseline bounds, and status from Garmin.",
        "daily_training_load": "Daily CTL (fitness), ATL (fatigue), and TSB (form) per sport type.",
        "daily_recovery": "Composite recovery score (0-100) with per-component breakdown (HRV, sleep, Body Battery, readiness, stress, fatigue).",
        "weekly_summary": "Pre-aggregated weekly totals: sessions, volume, TSS, zone distributions.",
        "anomaly_log": "Detected training/health anomalies with severity, z-score, and acknowledgement status.",
        "sync_state": "Last successful sync timestamp per data source (garmin_health, garmin_activities).",
        "schema_version": "Applied schema migration versions.",
    }

    tables = []
    for row in rows:
        name = row["table_name"]
        tables.append(
            {
                "table": name,
                "description": _TABLE_DESCRIPTIONS.get(name, ""),
                "estimated_rows": row.get("estimated_size"),
            }
        )

    return _json({"tables": tables, "total": len(tables)})


@mcp.resource(
    "schema://table/{table_name}",
    name="schema-table",
    description=(
        "Detailed schema for a single database table: column names, types, "
        "nullability, and a few sample values. Use before writing SQL against that table."
    ),
    mime_type="application/json",
)
def schema_table_resource(table_name: str) -> str:
    """Return column-level schema and sample rows for *table_name*."""
    db = _get_db()

    cols = db.fetchdf(
        "SELECT column_name, data_type, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_name = ? AND table_schema = 'main' "
        "ORDER BY ordinal_position",
        [table_name],
    ).to_dict(orient="records")  # type: ignore[union-attr]

    if not cols:
        return _json({"error": f"Table '{table_name}' not found. Check schema://tables for valid names."})

    try:
        sample_df = db.fetchdf(f"SELECT * FROM {table_name} LIMIT 3")  # noqa: S608
        samples = sample_df.to_dict(orient="records")  # type: ignore[union-attr]
    except Exception:
        samples = []

    return _json(
        {
            "table": table_name,
            "columns": cols,
            "sample_rows": samples,
        }
    )


# =========================================================================
# DATA QUERY TOOLS
# =========================================================================


@_tool()
def get_activities(
    sport_type: str = "all",
    start_date: str = "",
    end_date: str = "",
    limit: int = 20,
    sort_by: str = "date",
) -> str:
    """Query activities with filters.

    Returns a JSON array of activity records with basic metrics.

    Parameters:
        sport_type: Filter by sport type ("swim", "bike", "run", "strength", "other", or "all" for no filter).
        start_date: ISO date string (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date string (YYYY-MM-DD). Defaults to today.
        limit: Maximum number of activities to return (default 20).
        sort_by: Sort order: "date" (default, newest first), "distance", "duration", or "tss".
    """
    try:
        from hart.storage.queries import get_activities as _qa

        db = _get_db()
        sd = _parse_date(start_date)
        ed = _parse_date(end_date)
        st = sport_type if sport_type and sport_type != "all" else None

        rows = _qa(db, sport_type=st, start_date=sd, end_date=ed, limit=limit, sort_by=sort_by)
        return _json({"count": len(rows), "activities": rows})
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_activity_detail(activity_id: str) -> str:
    """Get full detail for a single activity including metrics, laps, and stream summary.

    Strength sessions also include every set in order (exercise, reps,
    weight, duration, rest after) under ``strength``.

    Parameters:
        activity_id: The unique activity identifier.
    """
    try:
        from hart.storage.queries import (
            get_activity_by_id,
            get_activity_laps,
            get_activity_streams,
        )

        db = _get_db()
        activity = get_activity_by_id(db, activity_id)
        if activity is None:
            return _json({"error": f"Activity {activity_id} not found"})

        laps = get_activity_laps(db, activity_id)

        # Summarise streams rather than returning raw data (too large for MCP)
        streams = get_activity_streams(db, activity_id)
        stream_summary: dict[str, Any] = {"total_points": len(streams)}
        if streams:
            import numpy as np

            hr_vals = [s["heart_rate"] for s in streams if s.get("heart_rate")]
            power_vals = [s["power"] for s in streams if s.get("power")]
            speed_vals = [s["speed"] for s in streams if s.get("speed")]
            alt_vals = [s["altitude"] for s in streams if s.get("altitude")]

            if hr_vals:
                stream_summary["hr"] = {
                    "min": int(min(hr_vals)),
                    "max": int(max(hr_vals)),
                    "avg": round(float(np.mean(hr_vals)), 1),
                    "samples": len(hr_vals),
                }
            if power_vals:
                stream_summary["power"] = {
                    "min": int(min(power_vals)),
                    "max": int(max(power_vals)),
                    "avg": round(float(np.mean(power_vals)), 1),
                    "samples": len(power_vals),
                }
            if speed_vals:
                stream_summary["speed_kmh"] = {
                    "min": round(min(speed_vals) * 3.6, 1),
                    "max": round(max(speed_vals) * 3.6, 1),
                    "avg": round(float(np.mean(speed_vals)) * 3.6, 1),
                }
            if alt_vals:
                stream_summary["altitude"] = {
                    "min": round(min(alt_vals), 1),
                    "max": round(max(alt_vals), 1),
                }
            duration_sec = streams[-1]["timestamp_sec"] - streams[0]["timestamp_sec"]
            stream_summary["duration_sec"] = round(duration_sec, 0)

        result: dict[str, Any] = {
            "activity": activity,
            "laps": laps,
            "stream_summary": stream_summary,
        }
        if activity.get("sport_type") == "strength":
            result["strength"] = _strength_summary(db, activity_id)
        return _json(result)
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


def _strength_summary(db: Any, activity_id: str) -> dict[str, Any]:
    """Per-exercise summary of a strength session's stored sets."""
    from hart.analytics.strength import summarize_strength_sets
    from hart.storage.queries import get_strength_sets

    sets = get_strength_sets(db, activity_id)
    if not sets:
        return {"sets_available": False, "note": "No set data stored — run `hart sync backfill-strength`."}
    return {"sets_available": True, **summarize_strength_sets(sets)}


@_tool()
def get_strength_history(
    days_back: int = 180,
    exercise: str = "",
) -> str:
    """Strength-training history: sessions and per-exercise progression.

    Returns every strength session in the window and, per lift, each
    session's individual sets (reps × weight, in order) so progression can be
    read set by set. Lifts are grouped like on the Strength page: Garmin's
    exercise and category-only names for the same lift are merged (e.g.
    "Barbell Deadlift" and "Deadlift"), plus any merges the athlete made;
    ``recorded_as`` lists the names Garmin actually stored.

    Parameters:
        days_back: How many days of history to include (default 180).
        exercise: Optional case-insensitive filter on exercise name or category
            (e.g. "deadlift", "calf_raise"). Empty for all exercises.
    """
    try:
        from hart.analytics.strength import exercise_label as recorded_label
        from hart.analytics.strength_progress import exercise_key, exercise_label, load_aliases

        db = _get_db()
        aliases = load_aliases(db)
        start = datetime.date.today() - datetime.timedelta(days=days_back)

        sessions = db.fetchdf(
            "SELECT a.activity_id, CAST(a.start_time AS DATE) AS date, a.name, "
            "a.elapsed_seconds, a.avg_hr, a.max_hr, a.calories, a.garmin_training_load, "
            "a.rpe, a.feel, "
            "COUNT(s.set_index) FILTER (WHERE s.set_type = 'active') AS active_sets "
            "FROM activities a LEFT JOIN strength_sets s USING (activity_id) "
            "WHERE a.sport_type = 'strength' AND CAST(a.start_time AS DATE) >= ? "
            "GROUP BY ALL ORDER BY date",
            [start],
        ).to_dict(orient="records")

        rows = db.fetchall(
            "SELECT CAST(a.start_time AS DATE) AS date, s.exercise_category, s.exercise_name, "
            "s.repetitions, s.weight_kg, ROUND(s.duration_sec) AS duration_sec "
            "FROM strength_sets s JOIN activities a USING (activity_id) "
            "WHERE s.set_type = 'active' AND CAST(a.start_time AS DATE) >= ? "
            "ORDER BY a.start_time, s.set_index",
            [start],
        )
        needle = exercise.strip().lower().replace(" ", "_")
        progression: dict[str, dict[str, Any]] = {}
        for d, cat, name, reps, kg, dur in rows:
            key = exercise_key(name, cat, aliases) or (name or cat or "unknown")
            if needle and not any(needle in (x or "").lower() for x in (key, name, cat)):
                continue
            lift = progression.setdefault(exercise_label(key), {"recorded_as": [], "sessions": []})
            recorded = recorded_label(cat, name)
            if recorded not in lift["recorded_as"]:
                lift["recorded_as"].append(recorded)
            history = lift["sessions"]
            if not history or history[-1]["date"] != d:
                history.append({"date": d, "sets": []})
            history[-1]["sets"].append({"reps": reps, "weight_kg": kg, "duration_sec": dur})

        return _json(
            {
                "period": {"start": start, "end": datetime.date.today()},
                "session_count": len(sessions),
                "sessions": sessions,
                "exercise_progression": progression,
            }
        )
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_training_load(
    start_date: str,
    end_date: str,
    sport_type: str = "combined",
) -> str:
    """Get daily CTL (fitness), ATL (fatigue), and TSB (form) values.

    Returns a JSON array of daily training load records for the Performance
    Management Chart (PMC).

    Parameters:
        start_date: ISO date string (YYYY-MM-DD).
        end_date: ISO date string (YYYY-MM-DD).
        sport_type: "combined" (default), "swim", "bike", "run", or "strength".
    """
    try:
        from hart.storage.queries import get_training_load as _qtl

        db = _get_db()
        sd = _parse_date(start_date) or _default_start(90)
        ed = _parse_date(end_date) or datetime.date.today()

        rows = _qtl(db, sd, ed, sport_type)
        return _json({"count": len(rows), "training_load": rows})
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_daily_health(start_date: str, end_date: str) -> str:
    """Get daily health metrics: resting HR, stress, Body Battery, VO2max, training readiness.

    Parameters:
        start_date: ISO date string (YYYY-MM-DD).
        end_date: ISO date string (YYYY-MM-DD).
    """
    try:
        from hart.storage.queries import get_daily_health as _qdh

        db = _get_db()
        sd = _parse_date(start_date) or _default_start(14)
        ed = _parse_date(end_date) or datetime.date.today()

        rows = _qdh(db, sd, ed)
        return _json({"count": len(rows), "health": rows})
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_sleep_data(start_date: str, end_date: str) -> str:
    """Get sleep records: duration, stages, scores, and overnight HRV.

    Parameters:
        start_date: ISO date string (YYYY-MM-DD).
        end_date: ISO date string (YYYY-MM-DD).
    """
    try:
        from hart.storage.queries import get_sleep_data as _qsd

        db = _get_db()
        sd = _parse_date(start_date) or _default_start(14)
        ed = _parse_date(end_date) or datetime.date.today()

        rows = _qsd(db, sd, ed)
        return _json({"count": len(rows), "sleep": rows})
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_hrv_trend(start_date: str, end_date: str) -> str:
    """Get HRV daily values and baseline trend data.

    Returns daily HRV measurements including last-night values, weekly averages,
    baseline bounds, and status.

    Parameters:
        start_date: ISO date string (YYYY-MM-DD).
        end_date: ISO date string (YYYY-MM-DD).
    """
    try:
        from hart.storage.queries import get_hrv_trend as _qhrv

        db = _get_db()
        sd = _parse_date(start_date) or _default_start(30)
        ed = _parse_date(end_date) or datetime.date.today()

        rows = _qhrv(db, sd, ed)
        return _json({"count": len(rows), "hrv": rows})
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_recovery_scores(start_date: str, end_date: str) -> str:
    """Get composite recovery scores with per-component breakdown.

    Returns daily recovery scores (0-100) with HRV, sleep, body battery,
    readiness, stress, and fatigue components.

    Parameters:
        start_date: ISO date string (YYYY-MM-DD).
        end_date: ISO date string (YYYY-MM-DD).
    """
    try:
        from hart.storage.queries import get_recovery_scores as _qrs

        db = _get_db()
        sd = _parse_date(start_date) or _default_start(14)
        ed = _parse_date(end_date) or datetime.date.today()

        rows = _qrs(db, sd, ed)
        return _json({"count": len(rows), "recovery": rows})
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_activity_metrics(
    sport_type: str = "",
    start_date: str = "",
    end_date: str = "",
) -> str:
    """Get computed metrics (TSS, zones, efficiency factor, decoupling) for activities.

    Parameters:
        sport_type: Filter by sport type (empty for all).
        start_date: ISO date string (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date string (YYYY-MM-DD). Defaults to today.
    """
    try:
        from hart.storage.queries import get_activity_metrics as _qam

        db = _get_db()
        sd = _parse_date(start_date)
        ed = _parse_date(end_date)
        st = sport_type if sport_type else None

        rows = _qam(db, sport_type=st, start_date=sd, end_date=ed)
        return _json({"count": len(rows), "metrics": rows})
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_weekly_summaries(
    start_date: str,
    end_date: str,
    sport_type: str = "",
) -> str:
    """Get weekly training summaries: volume, TSS, zones, efficiency.

    Parameters:
        start_date: ISO date string (YYYY-MM-DD).
        end_date: ISO date string (YYYY-MM-DD).
        sport_type: Filter by sport type (empty for all sports).
    """
    try:
        from hart.storage.queries import get_weekly_summaries as _qws

        db = _get_db()
        sd = _parse_date(start_date) or _default_start(56)
        ed = _parse_date(end_date) or datetime.date.today()
        st = sport_type if sport_type else None

        rows = _qws(db, sd, ed, sport_type=st)
        return _json({"count": len(rows), "summaries": rows})
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_anomalies(
    start_date: str = "",
    end_date: str = "",
    severity: str = "",
) -> str:
    """Get detected anomalies in training and health data.

    Parameters:
        start_date: ISO date string (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date string (YYYY-MM-DD). Defaults to today.
        severity: Filter by severity: "info", "warning", "critical", or empty for all.
    """
    try:
        from hart.storage.queries import get_anomalies as _qan

        db = _get_db()
        sd = _parse_date(start_date)
        ed = _parse_date(end_date)
        sev = severity if severity else None

        rows = _qan(db, start_date=sd, end_date=ed, severity=sev)
        return _json({"count": len(rows), "anomalies": rows})
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_athlete_profile() -> str:
    """Get the current athlete profile summary.

    Returns thresholds (FTP, CSS, FTPace), recent VO2max values, resting HR,
    current CTL/ATL/TSB, and latest recovery score.
    """
    try:
        from hart.storage.queries import get_athlete_profile as _qap

        db = _get_db()

        profile = _qap(db)

        from hart.server import settings

        profile["name"] = settings.get(db, "athlete_name")
        profile["power_single_sided"] = settings.get(db, "power_single_sided")

        return _json(profile)
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def run_sql_query(sql: str) -> str:
    """Execute a read-only SQL query against the analytics database.

    Only a single SELECT over database tables is allowed (file-reading table
    functions and paths are rejected). Maximum 100 rows returned.
    Use this for ad-hoc queries that the pre-built tools don't cover.

    Available tables: activities, activity_metrics, activity_streams,
    activity_laps, strength_sets, daily_training_load, daily_health,
    sleep_records, hrv_daily, daily_recovery, weekly_summary, anomaly_log,
    sync_state, races, annotations, athlete_notes, training_phases,
    planned_sessions, session_feedback, session_grades, daily_suggestions,
    jobs, lab_results, health_checks.

    Parameters:
        sql: The SQL SELECT query to execute.
    """
    try:
        from hart.storage.queries import run_analytics_query

        db = _get_db()
        rows = run_analytics_query(db, sql, max_rows=100)
        return _json({"count": len(rows), "rows": rows})
    except ValueError as exc:
        return _json({"error": str(exc)})
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


# =========================================================================
# ANALYTICS TOOLS
# =========================================================================


@_tool()
def analyze_activity(activity_id: str) -> str:
    """Run full analysis on a specific activity.

    Computes TSS, zone distribution, efficiency factor, aerobic decoupling,
    and compares to recent similar activities. Returns comprehensive JSON.

    Parameters:
        activity_id: The unique activity identifier.
    """
    try:
        from hart.storage.queries import (
            get_activities,
            get_activity_by_id,
            get_activity_streams,
        )
        from hart.storage.queries import (
            get_activity_metrics as _qam,
        )

        db = _get_db()

        activity = get_activity_by_id(db, activity_id)
        if activity is None:
            return _json({"error": f"Activity {activity_id} not found"})

        result: dict[str, Any] = {"activity": activity}

        # Existing computed metrics
        metrics_list = _qam(db, activity_ids=[activity_id])
        if metrics_list:
            result["metrics"] = metrics_list[0]

        # Stream-based analysis
        streams = get_activity_streams(db, activity_id)
        sport = activity.get("sport_type", "other")

        if streams:
            import numpy as np

            power_series = [s["power"] for s in streams if s.get("power")]

            # Power-duration curve for bike activities
            if sport == "bike" and power_series:
                from hart.analytics.power import (
                    normalized_power as _np_calc,
                )
                from hart.analytics.power import (
                    power_duration_curve,
                )

                result["power_duration_curve"] = power_duration_curve(power_series)
                result["normalized_power"] = round(_np_calc(power_series), 1)

            # Aerobic decoupling over the whole stream (aligned samples, no
            # duration/steadiness gate — the stored metric applies those).
            if sport in ("bike", "run"):
                from hart.analytics.efficiency import steady_session_decoupling

                field = "power" if sport == "bike" else "speed"
                decoup = steady_session_decoupling(
                    [s["timestamp_sec"] for s in streams],
                    [s.get("heart_rate") for s in streams],
                    [s.get(field) for s in streams],
                    sport,
                    min_moving_sec=20,
                    warmup_sec=0,
                    require_steady=False,
                )
                if decoup is not None:
                    result["computed_decoupling_pct"] = round(decoup, 2)

        # Compare to recent similar activities (last 30 days, same sport)
        recent = get_activities(
            db,
            sport_type=sport if sport != "other" else None,
            start_date=datetime.date.today() - datetime.timedelta(days=30),
            limit=10,
            sort_by="date",
        )
        # Exclude this activity from comparison
        recent = [a for a in recent if a.get("activity_id") != activity_id]
        if recent:
            import numpy as np

            comparison: dict[str, Any] = {"recent_count": len(recent)}

            tss_vals = [a["tss"] for a in recent if a.get("tss")]
            if tss_vals:
                comparison["avg_tss"] = round(float(np.mean(tss_vals)), 1)
            dur_vals = [a["elapsed_seconds"] for a in recent if a.get("elapsed_seconds")]
            if dur_vals:
                comparison["avg_duration_sec"] = round(float(np.mean(dur_vals)), 0)
            ef_vals = [a["efficiency_factor"] for a in recent if a.get("efficiency_factor")]
            if ef_vals:
                comparison["avg_ef"] = round(float(np.mean(ef_vals)), 3)
            dist_vals = [a["distance_meters"] for a in recent if a.get("distance_meters")]
            if dist_vals:
                comparison["avg_distance_m"] = round(float(np.mean(dist_vals)), 0)

            result["comparison_to_recent"] = comparison

        return _json(result)
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_power_curve(days_back: int = 90) -> str:
    """Get best power for various durations over a recent period.

    Scans all bike activities with stream data in the lookback window
    and returns the best average power for standard durations (1s to 2h).

    Parameters:
        days_back: Number of days to look back (default 90).
    """
    try:
        from hart.analytics.power import power_duration_curve
        from hart.storage.queries import get_activities, get_activity_streams

        db = _get_db()
        cutoff = datetime.date.today() - datetime.timedelta(days=days_back)

        bike_activities = get_activities(
            db,
            sport_type="bike",
            start_date=cutoff,
            limit=200,
            sort_by="date",
        )

        if not bike_activities:
            return _json({"error": "No bike activities found in the specified period"})

        # Aggregate best power across all activities
        best_power: dict[int, float] = {}
        activities_with_power = 0

        for act in bike_activities:
            streams = get_activity_streams(db, act["activity_id"])
            power_series = [s["power"] for s in streams if s.get("power") and s["power"] > 0]

            if len(power_series) < 30:
                continue

            activities_with_power += 1
            pdc = power_duration_curve(power_series)

            for duration, power in pdc.items():
                if duration not in best_power or power > best_power[duration]:
                    best_power[duration] = power

        # Format durations as human-readable labels
        duration_labels = {
            1: "1s",
            5: "5s",
            10: "10s",
            30: "30s",
            60: "1min",
            120: "2min",
            300: "5min",
            600: "10min",
            1200: "20min",
            1800: "30min",
            3600: "60min",
            5400: "90min",
            7200: "120min",
        }

        formatted = {}
        for dur_sec in sorted(best_power.keys()):
            label = duration_labels.get(dur_sec, f"{dur_sec}s")
            formatted[label] = best_power[dur_sec]

        return _json(
            {
                "days_back": days_back,
                "activities_analysed": activities_with_power,
                "power_curve": formatted,
                "power_curve_raw_seconds": best_power,
            }
        )
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def compare_periods(
    period1_start: str,
    period1_end: str,
    period2_start: str,
    period2_end: str,
    sport_type: str = "all",
) -> str:
    """Compare two training periods side-by-side.

    Compares volume, TSS, zone distribution, efficiency, and training load
    between two arbitrary date ranges.

    Parameters:
        period1_start: Start of first period (YYYY-MM-DD).
        period1_end: End of first period (YYYY-MM-DD).
        period2_start: Start of second period (YYYY-MM-DD).
        period2_end: End of second period (YYYY-MM-DD).
        sport_type: Filter by sport type ("all" for combined).
    """
    try:
        from hart.storage.queries import (
            get_activities,
        )
        from hart.storage.queries import (
            get_activity_metrics as _qam,
        )
        from hart.storage.queries import (
            get_training_load as _qtl,
        )

        db = _get_db()
        p1s = _parse_date(period1_start)
        p1e = _parse_date(period1_end)
        p2s = _parse_date(period2_start)
        p2e = _parse_date(period2_end)

        if not all([p1s, p1e, p2s, p2e]):
            return _json({"error": "All four dates are required in YYYY-MM-DD format"})

        st = sport_type if sport_type and sport_type != "all" else None

        def _period_stats(start: datetime.date, end: datetime.date) -> dict[str, Any]:
            import numpy as np

            activities = get_activities(db, sport_type=st, start_date=start, end_date=end, limit=500)
            metrics = _qam(db, sport_type=st, start_date=start, end_date=end)
            load = _qtl(db, start, end, sport_type="combined")

            stats: dict[str, Any] = {
                "date_range": f"{start.isoformat()} to {end.isoformat()}",
                "days": (end - start).days + 1,
                "activity_count": len(activities),
            }

            if activities:
                total_dur = sum(a.get("elapsed_seconds", 0) or 0 for a in activities)
                total_dist = sum(a.get("distance_meters", 0) or 0 for a in activities)
                total_elev = sum(a.get("total_elevation_m", 0) or 0 for a in activities)
                stats["total_duration_sec"] = total_dur
                stats["total_duration_hours"] = round(total_dur / 3600, 1)
                stats["total_distance_km"] = round(total_dist / 1000, 1)
                stats["total_elevation_m"] = round(total_elev, 0)

                # Sport breakdown
                sport_counts: dict[str, int] = {}
                for a in activities:
                    s = a.get("sport_type", "other")
                    sport_counts[s] = sport_counts.get(s, 0) + 1
                stats["sport_breakdown"] = sport_counts

            if metrics:
                tss_vals = [m["tss"] for m in metrics if m.get("tss")]
                ef_vals = [m["efficiency_factor"] for m in metrics if m.get("efficiency_factor")]
                dec_vals = [m["aerobic_decoupling_pct"] for m in metrics if m.get("aerobic_decoupling_pct")]

                if tss_vals:
                    stats["total_tss"] = round(sum(tss_vals), 1)
                    stats["avg_tss"] = round(float(np.mean(tss_vals)), 1)
                if ef_vals:
                    stats["avg_ef"] = round(float(np.mean(ef_vals)), 3)
                if dec_vals:
                    stats["avg_decoupling_pct"] = round(float(np.mean(dec_vals)), 1)

            if load:
                stats["end_ctl"] = load[-1].get("ctl")
                stats["end_atl"] = load[-1].get("atl")
                stats["end_tsb"] = load[-1].get("tsb")

            return stats

        p1_stats = _period_stats(p1s, p1e)
        p2_stats = _period_stats(p2s, p2e)

        # Compute deltas
        deltas: dict[str, Any] = {}
        for key in [
            "activity_count",
            "total_duration_hours",
            "total_distance_km",
            "total_tss",
            "avg_tss",
            "avg_ef",
            "avg_decoupling_pct",
            "end_ctl",
            "end_atl",
            "end_tsb",
        ]:
            v1 = p1_stats.get(key)
            v2 = p2_stats.get(key)
            if v1 is not None and v2 is not None:
                deltas[key] = {
                    "period1": v1,
                    "period2": v2,
                    "change": round(v2 - v1, 2),
                    "change_pct": round((v2 - v1) / v1 * 100, 1) if v1 != 0 else None,
                }

        return _json(
            {
                "period1": p1_stats,
                "period2": p2_stats,
                "deltas": deltas,
            }
        )
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


# =========================================================================
# SYNC & DATA TOOLS
# =========================================================================


def _run_sync(manual_days: int) -> dict[str, Any]:
    """Enqueue a sync in hart server, or run the pipeline inline (stdio mode)."""
    if _enqueue_job is not None:
        return _enqueue_job("sync", {"manual": True}, trigger="manual", dedupe_key="sync")

    from hart.server.jobs.pipeline import run_sync_pipeline

    return run_sync_pipeline(_get_db(), _get_config(), health_days=manual_days, activity_days=manual_days)


@_tool()
def sync_garmin() -> str:
    """Trigger a Garmin Connect sync (health, sleep, HRV and activities).

    Runs the full sync pipeline: Garmin data, training load, recovery scores,
    views and anomaly detection.  When connected to hart server the sync runs
    as a background job: the result contains ``job_id`` — call
    ``get_job_status(job_id)`` to see when it finished.  If a sync is already
    running, no new one starts: ``status`` is ``already_running`` and
    ``job_id`` points at the running sync.
    """
    try:
        return _json(_run_sync(manual_days=7))
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def sync_all() -> str:
    """Run all sync operations plus the analytics pipeline.

    Same as ``sync_garmin``: Garmin health and activities, then training
    load, recovery scores, materialised views and anomaly detection.  When
    connected to hart server it returns ``job_id``; poll ``get_job_status``.
    """
    try:
        return _json(_run_sync(manual_days=7))
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


# =========================================================================
# ATHLETE CONTEXT, SEASON, READINESS  (hart server data)
# =========================================================================


@_tool()
def get_athlete_context() -> str:
    """Get everything known about the athlete — call this FIRST in any analysis.

    Returns active athlete notes grouped by category (injuries, constraints,
    baselines, preferences, goals, health, equipment — some carry
    machine-checked ``rules``), races, the current training phase, and
    active annotations (injury, illness, no-watch periods, events).
    Notes awaiting approval are listed separately and must not be treated
    as facts until approved.
    """
    try:
        from hart.server.data import phase_on, rows

        db = _get_db()
        today = datetime.date.today()
        notes = rows(
            db,
            "SELECT id, category, title, body, valid_from, valid_to, rules, status, target_id, proposed_action "
            "FROM athlete_notes WHERE status IN ('active', 'proposed') ORDER BY category, id",
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for n in notes:
            if isinstance(n.get("rules"), str):
                n["rules"] = json.loads(n["rules"])
            if n["status"] == "active":
                grouped.setdefault(n["category"], []).append(
                    {
                        k: v
                        for k, v in n.items()
                        if k not in ("status", "category", "target_id", "proposed_action") and v is not None
                    }
                )
        return _json(
            {
                "today": today,
                "notes": grouped,
                "awaiting_approval": [
                    (
                        f"{n['proposed_action']} note #{n['target_id']}: {n['title']}"
                        if n.get("proposed_action") in ("update", "archive")
                        else n["title"]
                    )
                    for n in notes
                    if n["status"] == "proposed"
                ],
                "races": rows(db, "SELECT name, race_date, distance, priority, notes FROM races ORDER BY race_date"),
                "current_phase": phase_on(db, today),
                "annotations": rows(
                    db,
                    "SELECT kind, label, start_date, end_date FROM annotations "
                    "WHERE end_date IS NULL OR end_date >= ? ORDER BY start_date",
                    [today - datetime.timedelta(days=120)],
                ),
            }
        )
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def propose_athlete_note(
    category: str,
    title: str,
    body: str,
    valid_from: str = "",
    valid_to: str = "",
) -> str:
    """Propose a new athlete note (to remember something about the athlete).

    Use this instead of any other memory: the note is stored as "proposed"
    and only becomes part of the athlete context after the athlete approves
    it on the Notes page. Keep it factual, one topic per note.

    Parameters:
        category: injury | constraint | baseline | preference | goal | health | equipment | other
        title: Short title (max 120 chars).
        body: Markdown text with the facts and their source/date.
        valid_from: Optional ISO date the fact starts applying.
        valid_to: Optional ISO date it stops applying.
    """
    categories = {"injury", "constraint", "baseline", "preference", "goal", "health", "equipment", "other"}
    if category not in categories:
        return _json({"error": f"category must be one of {sorted(categories)}"})
    if not title.strip() or not body.strip() or len(title) > 120:
        return _json({"error": "title (max 120 chars) and body are required"})
    try:
        db = _get_db()
        new_id = db.fetchone(
            "INSERT INTO athlete_notes (category, title, body, valid_from, valid_to, status, source) "
            "VALUES (?, ?, ?, ?, ?, 'proposed', 'claude_proposed') RETURNING id",
            [category, title.strip(), body.strip(), _parse_date(valid_from), _parse_date(valid_to)],
        )[0]
        return _json(
            {
                "id": new_id,
                "status": "proposed",
                "message": "Saved as a proposal; the athlete approves it on the Notes page.",
            }
        )
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def propose_note_change(
    note_id: int,
    action: str,
    reason: str,
    category: str = "",
    title: str = "",
    body: str = "",
    valid_from: str = "",
    valid_to: str = "",
) -> str:
    """Propose editing or archiving an existing athlete note; nothing changes until the athlete approves.

    Use it when a note is outdated, wrong or duplicated — e.g. an injury has healed (archive it, or update it
    to say it healed), a baseline has a newer value, two notes say the same thing. Take the note ids from
    `get_athlete_context`. For a new fact use `propose_athlete_note` instead.

    Parameters:
        note_id: Id of the active note to change.
        action: "update" (only the fields you pass change; pass the full new body when changing the text)
            or "archive" (retire the note; the athlete can restore it later).
        reason: Why — shown to the athlete. Required.
        category, title, body, valid_from, valid_to: New values for "update" (ISO dates; "none" clears a date).
    """
    categories = {"injury", "constraint", "baseline", "preference", "goal", "health", "equipment", "other"}
    if action not in ("update", "archive"):
        return _json({"error": "action must be 'update' or 'archive'"})
    if not reason.strip():
        return _json({"error": "a reason is required"})
    try:
        db = _get_db()
        row = db.fetchone(
            "SELECT category, title, body, valid_from, valid_to, rules FROM athlete_notes "
            "WHERE id = ? AND status = 'active'",
            [note_id],
        )
        if row is None:
            return _json({"error": f"No active note with id {note_id}"})
        current = dict(zip(("category", "title", "body", "valid_from", "valid_to", "rules"), row))
        new = dict(current)
        if action == "update":
            if category:
                if category not in categories:
                    return _json({"error": f"category must be one of {sorted(categories)}"})
                new["category"] = category
            if title.strip():
                if len(title) > 120:
                    return _json({"error": "title is limited to 120 characters"})
                new["title"] = title.strip()
            if body.strip():
                new["body"] = body.strip()
            for key, value in (("valid_from", valid_from), ("valid_to", valid_to)):
                if value.strip().lower() == "none":
                    new[key] = None
                elif value.strip():
                    new[key] = _parse_date(value)
            if all(new[k] == current[k] for k in ("category", "title", "body", "valid_from", "valid_to")):
                return _json({"error": "nothing would change — pass the new values"})
        db.execute(
            "DELETE FROM athlete_notes WHERE status = 'proposed' AND target_id = ?", [note_id]
        )  # one pending change per note: the newest wins
        new_id = db.fetchone(
            "INSERT INTO athlete_notes (category, title, body, valid_from, valid_to, rules, status, source, "
            "target_id, proposed_action, proposal_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, 'proposed', 'claude_proposed', ?, ?, ?) RETURNING id",
            [
                new["category"],
                new["title"],
                new["body"],
                new["valid_from"],
                new["valid_to"],
                current["rules"]
                if isinstance(current["rules"], str) or current["rules"] is None
                else json.dumps(current["rules"]),
                note_id,
                action,
                reason.strip(),
            ],
        )[0]
        verb = "Archive" if action == "archive" else "Change"
        return _json(
            {
                "id": new_id,
                "status": "proposed",
                "action": action,
                "target_id": note_id,
                "summary": f"{verb} note “{current['title']}”",
                "message": "Proposed — the athlete approves it on the Notes page (or in the chat card).",
            }
        )
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_lab_results(marker: str = "", since: str = "") -> str:
    """Get blood / lab test results (2019 onwards), one row per marker per test date.

    Markers have the name printed by the lab (any language) plus a catalogue key for the
    important ones: hemoglobin, hematocrit, rbc, mcv, mch, mchc, rdw_cv, wbc,
    platelets, ferritin, iron, tsh, ft4, ft3, anti_tpo, anti_tg,
    vitamin_d_25oh, vitamin_b12, crp, esr, creatinine, egfr, urea, uric_acid,
    sodium, potassium, alt, ast, ldh, ck, glucose, cholesterol_total, hdl, ldl,
    non_hdl, triglycerides, testosterone, cortisol, epo. Each row has the value as printed (e.g. "<8.00"), the
    numeric value, unit, reference range and the lab's flag.
    With no marker: returns every test date with a count and its flagged
    markers, so you can see what exists. For health topics, explain and
    suggest questions for the doctor — never diagnose.

    Parameters:
        marker: Marker key (e.g. "ferritin") or part of the lab name (e.g. "hemoglob").
        since: Optional ISO date; only results on or after it.
    """
    try:
        from hart.server.data import rows

        db = _get_db()
        since_d = _parse_date(since)
        if not marker.strip():
            panels = rows(
                db,
                "SELECT test_date, count(*) AS markers, "
                "list(marker_name ORDER BY marker_name) FILTER (WHERE flag IS NOT NULL) AS flagged "
                "FROM lab_results WHERE test_date >= coalesce(?, DATE '1900-01-01') "
                "GROUP BY test_date ORDER BY test_date",
                [since_d],
            )
            return _json({"panels": panels, "tip": "Call get_lab_results(marker=...) for a marker's history."})
        needle = marker.strip().lower()
        results = rows(
            db,
            "SELECT test_date, marker_name, marker_key, value_text, value_num, qualifier, unit, "
            "ref_low, ref_high, ref_text, flag FROM lab_results "
            "WHERE (marker_key = ? OR lower(marker_name) LIKE ?) AND test_date >= coalesce(?, DATE '1900-01-01') "
            "ORDER BY marker_name, test_date",
            [needle, f"%{needle}%", since_d],
        )
        return _json({"marker": marker, "count": len(results), "results": results})
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_health_checks(status: str = "") -> str:
    """Get health-check reminders: due blood panels, follow-ups on flagged or
    near-limit lab results, planned re-checks from notes, stale markers
    (e.g. vitamin D), the pre-race medical exam, physio check-ins.

    Each check has kind, title, due date, markers, status (proposed | open |
    snoozed | done | dismissed), source (rule | manual | claude_proposed) and
    a rationale computed from the results. Informational: explain and suggest
    questions for the doctor — never diagnose.

    Parameters:
        status: Optional filter; default returns open, snoozed and proposed checks.
    """
    try:
        from hart.server.health import checks

        found = checks(_get_db(), status or None)
        if not status:
            found = [c for c in found if c["status"] in ("open", "snoozed", "proposed")]
        return _json({"today": datetime.date.today(), "checks": found})
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def propose_health_check(
    title: str, rationale: str, kind: str = "other", due_date: str = "", markers: list[str] | None = None
) -> str:
    """Propose a health check (e.g. a lab re-test or a doctor's visit); the
    athlete approves or dismisses it on the Health page.

    Parameters:
        title: Short title, e.g. "Re-test ferritin".
        rationale: Why — cite the result or note it comes from. Required. Never diagnose.
        kind: lab_panel | follow_up | medical_exam | physio | other.
        due_date: ISO date it should happen by (optional).
        markers: Catalogue keys for lab checks (e.g. ["ferritin", "hemoglobin"]).
    """
    try:
        from pydantic import ValidationError

        from hart.server.health import CheckIn, HealthError, create_check

        try:
            body = CheckIn(
                kind=kind, title=title, rationale=rationale, due_date=_parse_date(due_date), markers=markers or []
            )
            if not rationale.strip():
                raise HealthError("rationale is required")
            check_id = create_check(_get_db(), body, source="claude_proposed")
        except (ValidationError, HealthError) as exc:
            return _json({"error": str(exc)})
        return _json(
            {
                "id": check_id,
                "status": "proposed",
                "title": body.title,
                "message": "Proposed — the athlete approves it on the Health page.",
            }
        )
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def propose_plan_change(
    action: str,
    reason: str,
    target_id: int = 0,
    date: str = "",
    sport_type: str = "",
    title: str = "",
    duration_min: int = 0,
    intensity: str = "",
    description: str = "",
) -> str:
    """Propose adding, changing or removing a planned session; the athlete applies or dismisses it.

    Nothing changes until the athlete approves (in the chat card or on the Plan
    page). Read the plan first with `get_planned_sessions` (ids, current values).
    When a coach plans the day, prefer adjusting over replacing, and say why.

    Parameters:
        action: "create", "update" (only the fields you pass change) or "delete".
        reason: Why — shown to the athlete. Required.
        target_id: Id of the planned session to update or delete.
        date: ISO date. sport_type: swim, bike, run, strength, other or rest.
        title: Short title. duration_min: planned minutes.
        intensity: recovery, endurance, tempo, threshold, vo2, strength or mixed.
        description: Structure / notes for the session.
    """
    try:
        from hart.server.season_ops import SeasonError, create_proposal

        payload = {
            "date": date,
            "sport_type": sport_type,
            "title": title,
            "duration_min": duration_min or None,
            "intensity": intensity,
            "description": description,
        }
        try:
            result = create_proposal(_get_db(), "plan", action, reason, target_id or None, payload)
        except SeasonError as exc:
            return _json({"error": str(exc)})
        return _json({**result, "message": "Proposed — the athlete applies it in the chat or on the Plan page."})
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def import_coach_plan(text: str, date: str = "", reason: str = "") -> str:
    """Hand the coach's plan the athlete pasted to you (a day or a week, any format or language) to the
    Plan page's plan reader — exactly as if they had pasted it in the Plan page's paste box.

    Use this — not propose_plan_change — whenever the athlete gives you their coach's training. Pass the
    coach's text unchanged. It waits for the reader (up to a few minutes) and returns the sessions it found.
    Nothing is saved: the athlete applies the import on the Plan page, where it replaces earlier coach
    sessions on the same dates (as a re-pasted week does). Tell them that.

    Parameters:
        text: The coach's text, verbatim (without your own words or the athlete's greeting).
        date: ISO date for workouts the text doesn't date (e.g. "today's training" → today). Optional.
        reason: One line shown with the proposal. Optional.
    """
    import hashlib
    import time

    if _enqueue_job is None or _lookup_job is None:
        return _json({"error": "Only available when connected to hart server — paste it on the Plan page."})
    try:
        if not text.strip():
            return _json({"error": "text is empty"})
        if date:
            datetime.date.fromisoformat(date)
        key = hashlib.sha1(f"{date}|{text}".encode()).hexdigest()[:16]
        job = _enqueue_job(
            "plan_import",
            {"text": text, "default_date": date or None, "reason": reason},
            trigger="chat",
            dedupe_key=f"plan_import:{key}",
        )
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            current = _lookup_job(int(job["job_id"])) or {}
            if current.get("status") == "ok":
                result = current.get("result") or {}
                return _json({**result, "message": "Proposed — the athlete applies it on the Plan page."})
            if current.get("status") == "error":
                return _json({"error": current.get("error") or "the plan reader failed"})
            time.sleep(1)
        return _json({"job_id": job["job_id"], "message": "Still reading — the import will appear on the Plan page."})
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def propose_season_change(
    kind: str,
    action: str,
    reason: str,
    target_id: int = 0,
    name: str = "",
    phase_type: str = "",
    start_date: str = "",
    end_date: str = "",
    goal: str = "",
    label: str = "",
    annotation_kind: str = "",
    race_date: str = "",
    distance: str = "",
    priority: str = "",
    notes: str = "",
) -> str:
    """Propose a change to the season calendar or races; the athlete applies or dismisses it.

    Nothing changes until the athlete approves it (in the chat card or on
    the Season page). Get ids and current values from `get_training_phase`
    or `run_sql_query` on training_phases / annotations first. Phases may
    not overlap: to move a boundary, propose updates to both neighbours.

    Parameters:
        kind: "phase" (training_phases), "annotation" (injury, illness, travel,
              no_device, event, race, other — dated context shown on charts) or
              "race" (races: the next A-race drives the phase plan; race days
              get no training suggestion).
        action: "create", "update" (only the fields you pass change) or "delete".
        reason: Why — shown to the athlete. Required.
        target_id: Id of the phase/annotation to update or delete.
        name, phase_type, start_date, end_date, goal: Phase fields
              (phase_type: comeback, base, build, peak, taper, race, transition).
        label, annotation_kind, start_date, end_date: Annotation fields
              (end_date empty = ongoing).
        name, race_date, distance, priority, notes: Race fields
              (distance: full, half, olympic, sprint, run, other; priority A, B or C).
    """
    try:
        from hart.server.season_ops import SeasonError, create_proposal

        if kind == "phase":
            payload = {
                "name": name,
                "phase_type": phase_type,
                "start_date": start_date,
                "end_date": end_date,
                "goal": goal,
            }
        elif kind == "race":
            payload = {"name": name, "race_date": race_date, "distance": distance, "priority": priority, "notes": notes}
        else:
            payload = {"label": label, "kind": annotation_kind, "start_date": start_date, "end_date": end_date}
        try:
            result = create_proposal(_get_db(), kind, action, reason, target_id or None, payload)
        except SeasonError as exc:
            return _json({"error": str(exc)})
        return _json({**result, "message": "Proposed — the athlete applies or dismisses it."})
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_session_grade(activity_id: str) -> str:
    """Get the latest grade for one session: letter A–E, three dimension scores
    (execution, response, context fit) with justifications, summary,
    highlights, concerns, verified citations, and the facts it was based on.
    """
    try:
        from hart.server.grading import latest_grade

        grade = latest_grade(_get_db(), activity_id)
        return _json(grade or {"error": f"{activity_id} has not been graded yet"})
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_session_grades(start_date: str = "", end_date: str = "") -> str:
    """List the latest grade of every session in a date range (default: last 30 days):
    date, sport, name, letter, overall score (1–5), session type, confidence, summary.
    Sessions that aren't graded show their status (ungraded with reason, or failed).
    """
    try:
        from hart.server.data import rows

        start = _parse_date(start_date) or _default_start(30)
        end = _parse_date(end_date) or datetime.date.today()
        return _json(
            rows(
                _get_db(),
                "SELECT CAST(a.start_time AS DATE) AS date, a.activity_id, a.sport_type, a.name, g.status, g.letter, "
                "g.overall_score, g.session_type, g.confidence, g.summary, g.ungraded_reason FROM activities a "
                "JOIN (SELECT *, row_number() OVER (PARTITION BY activity_id ORDER BY version DESC) AS rn "
                "      FROM session_grades) g ON g.activity_id = a.activity_id AND g.rn = 1 "
                "WHERE a.start_time >= ? AND a.start_time < ? + INTERVAL 1 DAY ORDER BY a.start_time",
                [start, end],
            )
        )
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_planned_sessions(start_date: str = "", end_date: str = "") -> str:
    """Get the training plan (coach sessions pasted on the Plan page, your own) and accepted suggestions
    for a date range (default: this week and the next two).

    Each row: date, sport, title, planned duration (min), intensity, source
    (coach_import | manual | suggestion_accepted), the coach's text verbatim
    in ``description``, the matched activity if it was done, and
    ``replaced_by`` when an accepted suggestion replaced it. A coaching platform's
    TSS/IF in the coach's text are on a different scale from our load — never
    compare them.
    """
    try:
        from hart.server.data import monday
        from hart.server.plan import plan_rows

        start = _parse_date(start_date) or monday(datetime.date.today())
        end = _parse_date(end_date) or start + datetime.timedelta(days=20)
        return _json(plan_rows(_get_db(), start, end))
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_daily_suggestion(date: str = "") -> str:
    """Get the latest training suggestion for a date (default today): readiness
    it was based on, recommendation (as_planned | modify | replace | rest |
    free_choice), suggested sessions with structure and rationale, cautions,
    summary and cited numbers. Suggestions are advice; the coach's plan takes
    priority.
    """
    try:
        from hart.server.suggestions import for_display

        d = _parse_date(date) or datetime.date.today()
        found = for_display(_get_db(), d)
        if found is None:
            return _json({"date": d, "suggestion": None, "message": "No suggestion for this date"})
        found.pop("context", None)  # the full bundle is large; the fields above summarise it
        return _json(found)
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_training_phase(date: str = "") -> str:
    """Get the planned training phase and the observed training state for a date.

    Returns the season phase (comeback, base, build, peak, taper, race,
    transition) with week number, the observed state from the load curve
    (building, maintaining, absorbing, detraining, overreaching risk) with
    its metrics (CTL, 7-day ramp, TSB, CTL change vs 28 days, training days),
    and flags where the load doesn't fit the phase. Thresholds are
    provisional because the load is Garmin's EPOC-based score.

    Parameters:
        date: ISO date (default today).
    """
    try:
        from hart.server.data import season_context
        from hart.server.state import get_thresholds

        db = _get_db()
        return _json(season_context(db, _parse_date(date) or datetime.date.today(), get_thresholds(db)))
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_readiness(date: str = "") -> str:
    """Get the deterministic morning readiness for a date: green / amber / red / unknown.

    Based on last night's sleep, HRV and resting HR vs their 28-day
    baselines, the recovery score, Garmin Training Readiness, yesterday's
    TSB, critical anomalies, and illness/injury annotations. Returns the
    level, the reason, every rule that fired, missing inputs, and the
    input values. "unknown" means not enough data (e.g. the watch wasn't
    worn) — never treat it as good.

    Parameters:
        date: ISO date (default today).
    """
    try:
        from hart.server.data import readiness_on
        from hart.server.state import get_thresholds

        db = _get_db()
        return _json(readiness_on(db, _parse_date(date) or datetime.date.today(), get_thresholds(db)))
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_job_status(job_id: int) -> str:
    """Show the status of a background job (e.g. a sync you enqueued).

    Returns the job's status (queued, running, ok, error), timing,
    error, and result summary.  Only available when connected to hart server.
    """
    if _lookup_job is None:
        return _json({"error": "Jobs exist only in hart server; this MCP server runs standalone."})
    try:
        job = _lookup_job(int(job_id))
        return _json(job if job else {"error": f"No job with id {job_id}"})
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_latest_sync_status() -> str:
    """Show when each data source was last synced and any errors.

    Returns sync state for garmin_health and garmin_activities.
    """
    try:
        from hart.storage.queries import get_sync_state

        db = _get_db()

        sources = ["garmin_health", "garmin_activities"]
        states: dict[str, Any] = {}

        for source in sources:
            state = get_sync_state(db, source)
            states[source] = state if state else {"status": "never_synced"}

        return _json(states)
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


# =========================================================================
# ACTION TOOLS
# =========================================================================


@_tool()
def send_discord_message(channel_type: str, message: str) -> str:
    """Send a message to a Discord channel.

    Used by agents to proactively push insights, alerts, or reports.

    Parameters:
        channel_type: "main" for the general channel, "alert" for the alert channel.
        message: The message text to send.
    """
    try:
        import httpx

        config = _get_config()
        bot_token = config.discord.bot_token

        if not bot_token:
            return _json({"error": "Discord bot token not configured"})

        channel_id = config.discord.alert_channel_id if channel_type == "alert" else config.discord.channel_id

        if not channel_id:
            return _json({"error": f"Discord {channel_type} channel ID not configured"})

        # Use Discord REST API directly (avoids needing an async bot running)
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        headers = {
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
        }
        payload = {"content": message[:2000]}

        response = httpx.post(url, headers=headers, json=payload, timeout=10.0)

        if response.status_code in (200, 201):
            return _json(
                {
                    "status": "sent",
                    "channel_type": channel_type,
                    "channel_id": channel_id,
                    "message_length": len(message),
                }
            )
        else:
            return _json(
                {
                    "error": f"Discord API returned {response.status_code}",
                    "detail": response.text[:500],
                }
            )
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def acknowledge_anomaly(anomaly_id: int) -> str:
    """Mark an anomaly as acknowledged.

    Parameters:
        anomaly_id: The numeric ID of the anomaly to acknowledge.
    """
    try:
        db = _get_db()
        db.execute(
            "UPDATE anomaly_log SET acknowledged = TRUE WHERE id = ?",
            [anomaly_id],
        )

        # Verify it was updated
        row = db.fetchone(
            "SELECT id, anomaly_type, severity, acknowledged FROM anomaly_log WHERE id = ?",
            [anomaly_id],
        )
        if row is None:
            return _json({"error": f"Anomaly {anomaly_id} not found"})

        return _json(
            {
                "status": "acknowledged",
                "anomaly_id": row[0],
                "anomaly_type": row[1],
                "severity": row[2],
                "acknowledged": row[3],
            }
        )
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


# =========================================================================
# CONTEXT TOOLS (pre-aggregated for proactive agents)
# =========================================================================


@_tool()
def get_new_activity_context(activity_id: str) -> str:
    """Get everything needed to analyse a newly synced activity.

    Returns the activity data, recent training load context, comparison
    to recent similar activities, zone distribution, and how the activity
    shifts CTL/ATL/TSB. This is the tool the post-activity agent calls
    to build a comprehensive analysis in a single round-trip.

    Parameters:
        activity_id: The unique activity identifier.
    """
    try:
        from hart.storage.queries import (
            get_activities,
            get_activity_by_id,
            get_activity_laps,
            get_activity_streams,
        )
        from hart.storage.queries import (
            get_activity_metrics as _qam,
        )
        from hart.storage.queries import (
            get_athlete_profile as _qap,
        )
        from hart.storage.queries import (
            get_training_load as _qtl,
        )

        db = _get_db()

        activity = get_activity_by_id(db, activity_id)
        if activity is None:
            return _json({"error": f"Activity {activity_id} not found"})

        context: dict[str, Any] = {"activity": activity}

        # Metrics
        metrics_list = _qam(db, activity_ids=[activity_id])
        if metrics_list:
            context["metrics"] = metrics_list[0]

        # Laps
        laps = get_activity_laps(db, activity_id)
        if laps:
            context["laps"] = laps

        # Stream summary
        streams = get_activity_streams(db, activity_id)
        if streams:
            import numpy as np

            hr_vals = [s["heart_rate"] for s in streams if s.get("heart_rate")]
            power_vals = [s["power"] for s in streams if s.get("power")]

            stream_info: dict[str, Any] = {"total_points": len(streams)}
            if hr_vals:
                stream_info["hr"] = {
                    "min": int(min(hr_vals)),
                    "max": int(max(hr_vals)),
                    "avg": round(float(np.mean(hr_vals)), 1),
                }
            if power_vals:
                stream_info["power"] = {
                    "min": int(min(power_vals)),
                    "max": int(max(power_vals)),
                    "avg": round(float(np.mean(power_vals)), 1),
                }
                # Power duration curve for bike
                if activity.get("sport_type") == "bike":
                    from hart.analytics.power import power_duration_curve

                    pdc = power_duration_curve(power_vals)
                    stream_info["power_duration_curve"] = pdc

            context["stream_summary"] = stream_info

        # Strength: per-exercise breakdown + previous strength session
        if activity.get("sport_type") == "strength":
            context["strength"] = _strength_summary(db, activity_id)
            prev = db.fetchone(
                "SELECT activity_id FROM activities WHERE sport_type = 'strength' "
                "AND start_time < ? ORDER BY start_time DESC LIMIT 1",
                [activity["start_time"]],
            )
            if prev:
                prev_act = get_activity_by_id(db, prev[0])
                context["previous_strength_session"] = {
                    "activity_id": prev[0],
                    "start_time": prev_act.get("start_time") if prev_act else None,
                    **_strength_summary(db, prev[0]),
                }

        # Training load context: current CTL/ATL/TSB and how this shifts it
        today = datetime.date.today()
        load_history = _qtl(db, today - datetime.timedelta(days=7), today, "combined")
        if load_history:
            context["training_load_pre"] = load_history[-1]

        # Recent similar activities (same sport, last 30 days)
        sport = activity.get("sport_type")
        if sport and sport != "other":
            recent = get_activities(
                db,
                sport_type=sport,
                start_date=today - datetime.timedelta(days=30),
                limit=10,
                sort_by="date",
            )
            recent = [a for a in recent if a.get("activity_id") != activity_id]
            if recent:
                import numpy as np

                comparison: dict[str, Any] = {"count": len(recent)}
                tss_vals = [a["tss"] for a in recent if a.get("tss")]
                dur_vals = [a["elapsed_seconds"] for a in recent if a.get("elapsed_seconds")]
                ef_vals = [a["efficiency_factor"] for a in recent if a.get("efficiency_factor")]

                if tss_vals:
                    comparison["avg_tss"] = round(float(np.mean(tss_vals)), 1)
                if dur_vals:
                    comparison["avg_duration_sec"] = round(float(np.mean(dur_vals)), 0)
                if ef_vals:
                    comparison["avg_ef"] = round(float(np.mean(ef_vals)), 3)

                context["recent_similar"] = comparison

        # Athlete profile (thresholds for context)
        profile = _qap(db)
        context["athlete_profile"] = {
            "vo2max_run": profile.get("vo2max_run"),
            "vo2max_cycle": profile.get("vo2max_cycle"),
            "resting_hr": profile.get("resting_hr"),
            "training_load": profile.get("training_load"),
        }

        return _json(context)
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_morning_briefing_data() -> str:
    """Get everything needed for a morning recovery briefing.

    Returns last night's sleep data, current HRV vs baseline, Body Battery,
    Training Readiness, recovery score, today's projected TSB, and recent
    training load trend. This is the tool the morning-recovery agent calls.
    """
    try:
        from hart.storage.queries import (
            get_daily_health as _qdh,
        )
        from hart.storage.queries import (
            get_hrv_trend as _qhrv,
        )
        from hart.storage.queries import (
            get_recovery_scores as _qrs,
        )
        from hart.storage.queries import (
            get_sleep_data as _qsd,
        )
        from hart.storage.queries import (
            get_training_load as _qtl,
        )

        db = _get_db()
        today = datetime.date.today()
        yesterday = today - datetime.timedelta(days=1)

        briefing: dict[str, Any] = {"date": today.isoformat()}

        # Last night's sleep
        sleep = _qsd(db, yesterday, today)
        if sleep:
            briefing["sleep"] = sleep[-1]

        # Current HRV vs baseline
        hrv = _qhrv(db, today - datetime.timedelta(days=7), today)
        if hrv:
            briefing["hrv_current"] = hrv[-1]
            if len(hrv) > 1:
                import numpy as np

                hrv_vals = [h["hrv_weekly_avg_ms"] for h in hrv if h.get("hrv_weekly_avg_ms")]
                if hrv_vals:
                    briefing["hrv_7d_avg"] = round(float(np.mean(hrv_vals)), 1)

        # Today's health metrics
        health = _qdh(db, yesterday, today)
        if health:
            latest_health = health[-1]
            briefing["health"] = {
                "resting_hr": latest_health.get("resting_hr"),
                "body_battery_start": latest_health.get("body_battery_start"),
                "body_battery_high": latest_health.get("body_battery_high"),
                "body_battery_low": latest_health.get("body_battery_low"),
                "training_readiness": latest_health.get("training_readiness"),
                "avg_stress": latest_health.get("avg_stress"),
                "vo2max_run": latest_health.get("vo2max_run"),
                "vo2max_cycle": latest_health.get("vo2max_cycle"),
            }

        # Recovery score
        recovery = _qrs(db, yesterday, today)
        if recovery:
            briefing["recovery"] = recovery[-1]

        # Training load: recent 7-day trend + today's projected TSB
        load = _qtl(db, today - datetime.timedelta(days=7), today, "combined")
        if load:
            briefing["training_load_trend"] = load
            briefing["current_ctl"] = load[-1].get("ctl")
            briefing["current_atl"] = load[-1].get("atl")
            briefing["current_tsb"] = load[-1].get("tsb")

        # Recent activity summary (last 3 days)
        from hart.storage.queries import get_activities

        recent_activities = get_activities(
            db,
            start_date=today - datetime.timedelta(days=3),
            limit=10,
            sort_by="date",
        )
        if recent_activities:
            briefing["recent_activities"] = [
                {
                    "name": a.get("name"),
                    "sport_type": a.get("sport_type"),
                    "start_time": a.get("start_time"),
                    "elapsed_seconds": a.get("elapsed_seconds"),
                    "tss": a.get("tss"),
                    "distance_meters": a.get("distance_meters"),
                }
                for a in recent_activities
            ]

        # Recovery trend (last 7 days)
        recovery_7d = _qrs(db, today - datetime.timedelta(days=7), today)
        if recovery_7d:
            from hart.analytics.recovery import recovery_trend

            trend = recovery_trend(
                [
                    {"date": r.get("date"), "recovery_score": r.get("recovery_score")}
                    for r in recovery_7d
                    if r.get("recovery_score") is not None
                ]
            )
            briefing["recovery_trend"] = {
                "direction": trend.get("direction"),
                "days_below_60": trend.get("days_below_60"),
            }

        return _json(briefing)
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_weekly_report_data() -> str:
    """Get pre-computed data for the weekly training report.

    Returns the full week's activities, training load progression, zone
    distributions, recovery trends, detected anomalies, and comparison
    to the previous week. This is the tool the weekly-report agent calls.
    """
    try:
        from hart.storage.queries import (
            get_activities,
        )
        from hart.storage.queries import (
            get_activity_metrics as _qam,
        )
        from hart.storage.queries import (
            get_anomalies as _qan,
        )
        from hart.storage.queries import (
            get_recovery_scores as _qrs,
        )
        from hart.storage.queries import (
            get_training_load as _qtl,
        )
        from hart.storage.queries import (
            get_weekly_summaries as _qws,
        )

        db = _get_db()
        today = datetime.date.today()

        # Current week bounds (Monday-based ISO week)
        week_start = today - datetime.timedelta(days=today.weekday())
        week_end = week_start + datetime.timedelta(days=6)

        # Previous week
        prev_week_start = week_start - datetime.timedelta(days=7)
        prev_week_end = week_start - datetime.timedelta(days=1)

        report: dict[str, Any] = {
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
        }

        # This week's activities
        activities = get_activities(
            db,
            start_date=week_start,
            end_date=week_end,
            limit=100,
            sort_by="date",
        )
        report["activities"] = activities
        report["activity_count"] = len(activities)

        # Activity metrics for this week
        metrics = _qam(db, start_date=week_start, end_date=week_end)
        report["activity_metrics"] = metrics

        # Aggregate this week
        if activities:
            total_duration = sum(a.get("elapsed_seconds", 0) or 0 for a in activities)
            total_distance = sum(a.get("distance_meters", 0) or 0 for a in activities)
            total_tss = sum(m.get("tss", 0) or 0 for m in metrics)

            sport_counts: dict[str, int] = {}
            sport_duration: dict[str, int] = {}
            for a in activities:
                s = a.get("sport_type", "other")
                sport_counts[s] = sport_counts.get(s, 0) + 1
                sport_duration[s] = sport_duration.get(s, 0) + (a.get("elapsed_seconds", 0) or 0)

            report["week_totals"] = {
                "sessions": len(activities),
                "duration_sec": total_duration,
                "duration_hours": round(total_duration / 3600, 1),
                "distance_km": round(total_distance / 1000, 1),
                "total_tss": round(total_tss, 1),
                "sport_sessions": sport_counts,
                "sport_duration_sec": sport_duration,
            }

        # Weekly summaries (if pre-computed)
        summaries = _qws(db, week_start, week_end)
        if summaries:
            report["weekly_summaries"] = summaries

        # Training load for this week
        load = _qtl(db, week_start, week_end, "combined")
        report["training_load"] = load

        # Recovery trend for this week
        recovery = _qrs(db, week_start, week_end)
        report["recovery_scores"] = recovery

        # Anomalies this week
        anomalies = _qan(db, start_date=week_start, end_date=week_end)
        report["anomalies"] = anomalies

        # Previous week comparison
        prev_activities = get_activities(
            db,
            start_date=prev_week_start,
            end_date=prev_week_end,
            limit=100,
            sort_by="date",
        )
        prev_metrics = _qam(db, start_date=prev_week_start, end_date=prev_week_end)

        if prev_activities:
            prev_duration = sum(a.get("elapsed_seconds", 0) or 0 for a in prev_activities)
            prev_distance = sum(a.get("distance_meters", 0) or 0 for a in prev_activities)
            prev_tss = sum(m.get("tss", 0) or 0 for m in prev_metrics)

            report["previous_week"] = {
                "week_start": prev_week_start.isoformat(),
                "sessions": len(prev_activities),
                "duration_sec": prev_duration,
                "duration_hours": round(prev_duration / 3600, 1),
                "distance_km": round(prev_distance / 1000, 1),
                "total_tss": round(prev_tss, 1),
            }

            # Deltas
            if activities:
                curr_dur = report["week_totals"]["duration_sec"]
                report["week_over_week"] = {
                    "sessions_delta": len(activities) - len(prev_activities),
                    "duration_delta_sec": curr_dur - prev_duration,
                    "duration_delta_pct": round((curr_dur - prev_duration) / prev_duration * 100, 1)
                    if prev_duration > 0
                    else None,
                    "tss_delta": round(report["week_totals"]["total_tss"] - prev_tss, 1),
                    "tss_delta_pct": round((report["week_totals"]["total_tss"] - prev_tss) / prev_tss * 100, 1)
                    if prev_tss > 0
                    else None,
                }

        # Training load at week boundaries (for CTL/ATL/TSB progression)
        load_extended = _qtl(
            db,
            week_start - datetime.timedelta(days=7),
            week_end,
            "combined",
        )
        if load_extended:
            report["ctl_progression"] = {
                "start_of_week": next(
                    (row for row in load_extended if row.get("date") and str(row["date"]) >= week_start.isoformat()),
                    None,
                ),
                "end_of_week": load_extended[-1] if load_extended else None,
            }

        return _json(report)
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


# =========================================================================
# ENHANCED TOOLS
# =========================================================================


@_tool()
def describe_schema(table_name: str = "") -> str:
    """Describe the database schema so you can write accurate SQL queries.

    With no argument: returns all table names with row counts and descriptions.
    With a table name: returns columns, data types, and 3 sample rows.

    Use this before calling run_sql_query when you need to know column names.

    Parameters:
        table_name: Table to describe (empty string returns all tables).
    """
    try:
        db = _get_db()

        _TABLE_DESCRIPTIONS: dict[str, str] = {
            "activities": "One row per session. Core data: sport, time, distance, HR, power, pace.",
            "strength_sets": "One row per strength set: set_type, reps, weight_kg, exercise category/name.",
            "activity_streams": "Second-by-second time-series: HR, power, cadence, speed, GPS, running form.",
            "activity_laps": "Lap-level summaries within activities.",
            "activity_metrics": "Computed metrics: TSS, intensity factor, zone distributions, efficiency, decoupling.",
            "hrv_samples": "Raw R-R interval samples from activity HRV.",
            "daily_health": "Daily Garmin health: resting HR, stress, Body Battery, readiness, VO2max, steps.",
            "sleep_records": "Nightly sleep: duration, stages (deep/light/REM), SpO2, score, overnight HRV.",
            "hrv_daily": "Daily HRV: last-night ms, 7-day rolling avg, baseline bounds, status.",
            "daily_training_load": "Daily CTL, ATL, TSB per sport type.",
            "daily_recovery": "Composite recovery score (0-100) with HRV/sleep/BB/readiness/stress/fatigue components.",
            "weekly_summary": "Pre-aggregated weekly totals: sessions, volume, TSS, zone distributions.",
            "anomaly_log": "Detected training/health anomalies with severity, z-score, acknowledged flag.",
            "sync_state": "Last sync timestamp per source (garmin_health, garmin_activities, pipeline).",
            "schema_version": "Applied schema migration versions.",
            "races": "Target races: name, date, distance, priority (A/B/C).",
            "annotations": "Dated periods for context: injury, illness, travel, no_device (watch not worn), event.",
            "athlete_notes": "Athlete context notes (injuries, constraints, baselines, preferences); status active/proposed/archived.",
            "training_phases": "Season phases (comeback, base, build, peak, taper, race, transition) with dates.",
            "planned_sessions": "Planned sessions per day (coach's plan pasted in, your own, accepted suggestions), matched to activities.",
            "session_feedback": "Athlete's own RPE (1-10), feel (1-5) and note (comment; NULL = the note is the Garmin description in activities.description) per activity.",
            "session_grades": "Session grades (A-E) with dimension scores, summary and citations; latest = max(version).",
            "daily_suggestions": "Daily training suggestions with readiness; latest = max(version) per date.",
            "chat_conversations": "Web chat conversations.",
            "chat_messages": "Web chat messages.",
            "claude_runs": "Log of every Claude run (chat, grading, suggestions).",
            "jobs": "Background jobs (sync, grading, suggestions) with status and result.",
            "lab_results": "Blood/lab results: one row per marker per test date, value, unit, reference range, flag.",
            "app_settings": "Server settings and runtime state.",
        }

        if not table_name:
            rows = db.fetchdf(
                "SELECT table_name, estimated_size FROM duckdb_tables() WHERE schema_name = 'main' ORDER BY table_name"
            ).to_dict(orient="records")  # type: ignore[union-attr]

            tables = [
                {
                    "table": r["table_name"],
                    "estimated_rows": r.get("estimated_size"),
                    "description": _TABLE_DESCRIPTIONS.get(r["table_name"], ""),
                }
                for r in rows
            ]
            return _json({"tables": tables, "tip": "Call describe_schema('tablename') for column details."})

        cols = db.fetchdf(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = ? AND table_schema = 'main' "
            "ORDER BY ordinal_position",
            [table_name],
        ).to_dict(orient="records")  # type: ignore[union-attr]

        if not cols:
            return _json(
                {
                    "error": f"Table '{table_name}' not found.",
                    "tip": "Call describe_schema() with no argument for valid table names.",
                }
            )

        try:
            sample_df = db.fetchdf(f"SELECT * FROM {table_name} LIMIT 3")  # noqa: S608
            samples = sample_df.to_dict(orient="records")  # type: ignore[union-attr]
        except Exception:
            samples = []

        return _json(
            {
                "table": table_name,
                "description": _TABLE_DESCRIPTIONS.get(table_name, ""),
                "columns": cols,
                "sample_rows": samples,
            }
        )
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_health(
    start_date: str,
    end_date: str,
    include: str = "daily,sleep,hrv,recovery",
) -> str:
    """Get health data for a date range — one call instead of four.

    Combines daily health metrics, sleep records, HRV trend, and recovery
    scores into a single response. Returns only the sections you request.

    Parameters:
        start_date: ISO date string (YYYY-MM-DD). Defaults to 14 days ago.
        end_date: ISO date string (YYYY-MM-DD). Defaults to today.
        include: Comma-separated subset of: daily, sleep, hrv, recovery.
                 Default is all four sections.
    """
    try:
        from hart.storage.queries import (
            get_daily_health as _qdh,
        )
        from hart.storage.queries import (
            get_hrv_trend as _qhrv,
        )
        from hart.storage.queries import (
            get_recovery_scores as _qrs,
        )
        from hart.storage.queries import (
            get_sleep_data as _qsd,
        )

        db = _get_db()
        sd = _parse_date(start_date) or _default_start(14)
        ed = _parse_date(end_date) or datetime.date.today()

        sections = {s.strip() for s in include.split(",") if s.strip()}
        result: dict[str, Any] = {
            "date_range": {"start": sd.isoformat(), "end": ed.isoformat()},
            "_sync": _sync_meta(),
        }

        if "daily" in sections:
            rows = _qdh(db, sd, ed)
            result["daily"] = {"count": len(rows), "data": rows}

        if "sleep" in sections:
            rows = _qsd(db, sd, ed)
            result["sleep"] = {"count": len(rows), "data": rows}

        if "hrv" in sections:
            rows = _qhrv(db, sd, ed)
            result["hrv"] = {"count": len(rows), "data": rows}

        if "recovery" in sections:
            rows = _qrs(db, sd, ed)
            result["recovery"] = {"count": len(rows), "data": rows}

        unknown = sections - {"daily", "sleep", "hrv", "recovery"}
        if unknown:
            result["warning"] = f"Unknown include sections ignored: {', '.join(sorted(unknown))}"

        return _json(result)
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_activity_streams(
    activity_id: str,
    metrics: str = "heart_rate,power,speed,altitude",
    start_pct: float = 0.0,
    end_pct: float = 1.0,
    downsample_to: int = 300,
) -> str:
    """Get time-series stream data for an activity.

    Returns per-second data for the requested metrics, optionally sliced to
    a portion of the activity and downsampled for analysis.

    Parameters:
        activity_id: The unique activity identifier.
        metrics: Comma-separated list of channels to return.
                 Available: heart_rate, power, cadence, speed, altitude,
                 distance, latitude, longitude, temperature, grade_percent,
                 ground_contact_time_ms, vertical_oscillation_mm,
                 vertical_ratio_pct, stride_length_m, respiration_rate.
        start_pct: Start of the slice as a fraction of total duration (0.0 = start).
        end_pct: End of the slice as a fraction of total duration (1.0 = end).
                 Example: start_pct=0.75, end_pct=1.0 = last quarter of activity.
        downsample_to: Maximum data points to return (default 300).
                       Set higher for finer resolution, lower for overview.
    """
    try:
        from hart.storage.queries import get_activity_streams as _qstreams

        db = _get_db()

        # Validate metric names against allowed columns
        _VALID_METRICS = {
            "heart_rate",
            "power",
            "cadence",
            "speed",
            "altitude",
            "distance",
            "latitude",
            "longitude",
            "temperature",
            "grade_percent",
            "ground_contact_time_ms",
            "vertical_oscillation_mm",
            "vertical_ratio_pct",
            "stride_length_m",
            "respiration_rate",
        }
        requested = [m.strip() for m in metrics.split(",") if m.strip()]
        valid = [m for m in requested if m in _VALID_METRICS]
        invalid = [m for m in requested if m not in _VALID_METRICS]

        streams = _qstreams(db, activity_id)
        if not streams:
            return _json({"error": f"No stream data found for activity {activity_id}"})

        # Slice by percentage of duration
        total = len(streams)
        start_idx = max(0, int(total * max(0.0, start_pct)))
        end_idx = min(total, int(total * min(1.0, end_pct)))
        if end_idx <= start_idx:
            return _json({"error": "start_pct must be less than end_pct"})
        sliced = streams[start_idx:end_idx]

        # Downsample evenly
        if downsample_to > 0 and len(sliced) > downsample_to:
            step = len(sliced) / downsample_to
            sliced = [sliced[int(i * step)] for i in range(downsample_to)]

        # Extract requested columns (always include timestamp_sec)
        output_cols = ["timestamp_sec"] + (valid or list(_VALID_METRICS))
        result_rows = [{col: row.get(col) for col in output_cols if col in row} for row in sliced]

        # Summary stats for each metric
        import numpy as np

        stats: dict[str, Any] = {}
        for col in valid:
            vals = [r[col] for r in sliced if r.get(col) is not None]
            if vals:
                stats[col] = {
                    "min": round(float(min(vals)), 2),
                    "max": round(float(max(vals)), 2),
                    "avg": round(float(np.mean(vals)), 2),
                    "samples": len(vals),
                }

        return _json(
            {
                "activity_id": activity_id,
                "total_stream_points": total,
                "slice": {
                    "start_pct": start_pct,
                    "end_pct": end_pct,
                    "points_in_slice": end_idx - start_idx,
                    "points_returned": len(sliced),
                },
                "metrics_requested": requested,
                "metrics_invalid": invalid,
                "stats": stats,
                "data": result_rows,
            }
        )
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


@_tool()
def get_training_timeline(
    start_date: str,
    end_date: str,
    include: str = "activities,load,health,anomalies",
) -> str:
    """Get a unified training timeline combining activities, load, health, and anomalies.

    Returns all requested data aligned by date in a single call, replacing
    the pattern of calling get_activities + get_training_load + get_daily_health
    + get_sleep_data separately.

    Parameters:
        start_date: ISO date string (YYYY-MM-DD).
        end_date: ISO date string (YYYY-MM-DD). Defaults to today.
        include: Comma-separated subset of: activities, load, health, anomalies.
                 Default is all four.
    """
    try:
        from hart.storage.queries import (
            get_activities as _qa,
        )
        from hart.storage.queries import (
            get_anomalies as _qan,
        )
        from hart.storage.queries import (
            get_daily_health as _qdh,
        )
        from hart.storage.queries import (
            get_hrv_trend as _qhrv,
        )
        from hart.storage.queries import (
            get_recovery_scores as _qrs,
        )
        from hart.storage.queries import (
            get_sleep_data as _qsd,
        )
        from hart.storage.queries import (
            get_training_load as _qtl,
        )

        db = _get_db()
        sd = _parse_date(start_date) or _default_start(14)
        ed = _parse_date(end_date) or datetime.date.today()

        sections = {s.strip() for s in include.split(",") if s.strip()}
        result: dict[str, Any] = {
            "date_range": {"start": sd.isoformat(), "end": ed.isoformat()},
            "_sync": _sync_meta(),
        }

        if "activities" in sections:
            activities = _qa(db, start_date=sd, end_date=ed, limit=500, sort_by="date")
            result["activities"] = {
                "count": len(activities),
                "data": activities,
            }

        if "load" in sections:
            load = _qtl(db, sd, ed, "combined")
            result["training_load"] = {
                "count": len(load),
                "sport_type": "combined",
                "data": load,
            }

        if "health" in sections:
            daily = _qdh(db, sd, ed)
            sleep = _qsd(db, sd, ed)
            hrv = _qhrv(db, sd, ed)
            recovery = _qrs(db, sd, ed)

            # Merge into date-keyed dict for easy alignment
            health_by_date: dict[str, dict[str, Any]] = {}
            for row in daily:
                d = str(row.get("date", ""))
                health_by_date.setdefault(d, {})["daily"] = row
            for row in sleep:
                d = str(row.get("date", ""))
                health_by_date.setdefault(d, {})["sleep"] = row
            for row in hrv:
                d = str(row.get("date", ""))
                health_by_date.setdefault(d, {})["hrv"] = row
            for row in recovery:
                d = str(row.get("date", ""))
                health_by_date.setdefault(d, {})["recovery"] = row

            result["health"] = {
                "days_with_data": len(health_by_date),
                "by_date": dict(sorted(health_by_date.items())),
            }

        if "anomalies" in sections:
            anomalies = _qan(db, start_date=sd, end_date=ed)
            result["anomalies"] = {
                "count": len(anomalies),
                "data": anomalies,
            }

        return _json(result)
    except Exception as exc:
        return _json({"error": str(exc), "traceback": traceback.format_exc()})


# =========================================================================
# MCP PROMPTS  (agent workflows — invoke to load the agent system prompt)
# =========================================================================


def _load_agent(name: str) -> str:
    """Read an agent markdown file, stripping the YAML front-matter."""
    path = _AGENTS_DIR / f"{name}.md"
    if not path.is_file():
        return f"Agent file not found: {path}"
    text = path.read_text(encoding="utf-8")
    # Strip YAML front-matter (--- ... ---)
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3 :].lstrip("\n")
    return text


@mcp.prompt(
    name="morning-recovery",
    description=(
        "Morning recovery briefing. Analyzes last night's sleep, HRV vs baseline, "
        "Body Battery, Training Readiness, and TSB. Call get_morning_briefing_data then "
        "send_discord_message with a 1-2 paragraph summary."
    ),
)
def morning_recovery_prompt() -> str:
    return _load_agent("morning-recovery")


@mcp.prompt(
    name="post-activity",
    description=(
        "Post-activity analysis. Call get_new_activity_context(activity_id) for the "
        "most recently synced activity, then analyze TSS, efficiency, aerobic decoupling, "
        "and comparison to recent similar sessions."
    ),
)
def post_activity_prompt(activity_id: str = "") -> str:
    base = _load_agent("post-activity")
    if activity_id:
        base = f"Activity ID to analyse: {activity_id}\n\n{base}"
    return base


@mcp.prompt(
    name="weekly-reporter",
    description=(
        "Weekly training report. Calls get_weekly_report_data for the current week, "
        "summarises sessions, volume, TSS, zone distribution, and week-over-week delta. "
        "Sends the report to Discord."
    ),
)
def weekly_reporter_prompt() -> str:
    return _load_agent("weekly-reporter")


@mcp.prompt(
    name="recovery-monitor",
    description=(
        "Recovery monitoring. Checks for multi-day HRV suppression, chronic sleep debt, "
        "and Body Battery failure to recharge. Sends an alert to Discord if thresholds breached."
    ),
)
def recovery_monitor_prompt() -> str:
    return _load_agent("recovery-monitor")


@mcp.prompt(
    name="anomaly-detector",
    description=(
        "Anomaly detection sweep. Scans recent activities and health data for unusual "
        "patterns (HR spikes, EF drops, sleep crashes) and logs them to anomaly_log."
    ),
)
def anomaly_detector_prompt() -> str:
    return _load_agent("anomaly-detector")


@mcp.prompt(
    name="performance-analyst",
    description=(
        "Deep performance analysis. Examines CTL/ATL/TSB progression, efficiency trends, "
        "aerobic decoupling trends, and training balance across disciplines."
    ),
)
def performance_analyst_prompt() -> str:
    return _load_agent("performance-analyst")


@mcp.prompt(
    name="training-load-analyst",
    description=(
        "Training load analysis. Analyzes CTL progression, ATL/TSB dynamics, "
        "monotony, strain, and compares current load to the 12-week trend."
    ),
)
def training_load_analyst_prompt() -> str:
    return _load_agent("training-load-analyst")


@mcp.prompt(
    name="nutrition-analyst",
    description=(
        "Race nutrition planning. Calculates per-hour carbohydrate, fluid, and sodium "
        "targets for a given race duration, intensity, and expected temperature."
    ),
)
def nutrition_analyst_prompt(duration_minutes: float = 0, temperature_c: float = 20) -> str:
    base = _load_agent("nutrition-analyst")
    if duration_minutes:
        base = f"Race duration: {duration_minutes} min, temperature: {temperature_c}°C\n\n{base}"
    return base


@mcp.prompt(
    name="race-strategist",
    description=(
        "Race strategy and pacing. Uses athlete thresholds, current fitness (CTL), "
        "and race course profile to recommend target paces and power by segment."
    ),
)
def race_strategist_prompt() -> str:
    return _load_agent("race-strategist")


# =========================================================================
# Entry point
# =========================================================================


def main() -> None:
    """Run the MCP server over stdio transport."""
    mcp.run()


if __name__ == "__main__":
    main()
