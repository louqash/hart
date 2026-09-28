"""Canonical sync pipeline.

This is the only implementation of "sync Garmin and bring every derived table
up to date".  The server's job runner, the MCP sync tools, and the CLI all go
through :func:`run_sync_pipeline`, so no trigger can skip a step.

Steps:
    1. Garmin health (daily health, sleep, HRV)
    2. Garmin activities (FIT streams, laps, strength sets, metrics)
    3. Training load (CTL/ATL/TSB)
    4. Recovery scores for every date touched in step 1
    5. Materialised views
    6. Anomaly detection (deduplicated inserts)
    7. Pipeline sync state

A failure in steps 1-2 doesn't stop steps 3-7: they run on whatever data
exists, and the result is marked ``partial``.
"""

from __future__ import annotations

import datetime
import logging
import time
from collections.abc import Callable
from typing import Any

from hart.config import HartSettings
from hart.storage.database import Database

logger = logging.getLogger(__name__)


def row_dict(db: Database, sql: str, params: list[Any] | None = None) -> dict[str, Any] | None:
    """Return the first row as a dict with real ``None`` for NULLs.

    ``Database.fetchdf`` turns NULL numbers into NaN, which the scoring
    functions would treat as present values.
    """
    cur = db.connection.execute(sql, params or [])
    row = cur.fetchone()
    if row is None:
        return None
    return {col[0]: val for col, val in zip(cur.description, row)}


def has_sleep_for(db: Database, date: datetime.date) -> bool:
    """True when the sleep record for the night ending on *date* exists."""
    row = db.fetchone(
        "SELECT 1 FROM sleep_records WHERE date = ? AND total_sleep_sec IS NOT NULL",
        [date],
    )
    return row is not None


# ---------------------------------------------------------------------------
# Step 4: recovery scores
# ---------------------------------------------------------------------------


def persist_recovery_scores(
    db: Database,
    dates: list[datetime.date] | None,
    weights: dict[str, float] | None = None,
) -> int:
    """Compute and store ``daily_recovery`` for *dates* (all dates if None).

    A date is scored only when at least one of daily health, sleep, or HRV
    exists for it.  The scoring functions fill missing inputs with a neutral
    50, so scoring a date without any real data would invent a number.
    """
    from hart.analytics.recovery import compute_recovery_score
    from hart.storage.writers import upsert_recovery_score

    if dates is None:
        dates = [
            r[0]
            for r in db.fetchall(
                "SELECT date FROM daily_health UNION "
                "SELECT date FROM sleep_records UNION "
                "SELECT date FROM hrv_daily ORDER BY 1"
            )
        ]

    written = 0
    for d in dates:
        health = row_dict(db, "SELECT * FROM daily_health WHERE date = ?", [d])
        sleep = row_dict(db, "SELECT * FROM sleep_records WHERE date = ?", [d])
        hrv_row = row_dict(db, "SELECT * FROM hrv_daily WHERE date = ?", [d])
        if health is None and sleep is None and hrv_row is None:
            continue

        hrv: dict[str, Any] = {}
        if hrv_row:
            hrv = {
                "hrv_last_night": hrv_row.get("hrv_last_night_ms"),
                "baseline_low": hrv_row.get("baseline_low_ms"),
                "baseline_high": hrv_row.get("baseline_high_ms"),
            }
            if hrv_row.get("hrv_status"):
                hrv["hrv_status"] = hrv_row["hrv_status"]
        load = (
            row_dict(
                db,
                "SELECT tsb FROM daily_training_load WHERE date = ? AND sport_type = 'combined'",
                [d],
            )
            or {}
        )

        score = compute_recovery_score(
            health={**(health or {}), "date": d},
            sleep=sleep or {},
            hrv=hrv,
            training_load=load,
            weights=weights,
        )
        upsert_recovery_score(db, score)
        written += 1
    return written


# ---------------------------------------------------------------------------
# Step 6: anomalies
# ---------------------------------------------------------------------------


def insert_new_anomalies(db: Database, lookback_days: int = 14) -> dict[str, int]:
    """Run anomaly detection and insert only anomalies not already logged."""
    from hart.analytics.anomaly import detect_anomalies
    from hart.storage.writers import insert_anomaly

    detected = detect_anomalies(db, lookback_days=lookback_days)
    inserted = 0
    for a in detected:
        exists = db.fetchone(
            "SELECT 1 FROM anomaly_log WHERE anomaly_type = ? AND metric_name = ? "
            "AND activity_id IS NOT DISTINCT FROM ? "
            "AND date_range_start IS NOT DISTINCT FROM ? "
            "AND date_range_end IS NOT DISTINCT FROM ? "
            "AND description = ?",
            [a.anomaly_type, a.metric_name, a.activity_id, a.date_range_start, a.date_range_end, a.description],
        )
        if exists is None:
            insert_anomaly(db, a)
            inserted += 1
    return {"detected": len(detected), "inserted": inserted}


# ---------------------------------------------------------------------------
# Garmin error classification
# ---------------------------------------------------------------------------


def _classify_garmin_error(exc: BaseException) -> str:
    name = type(exc).__name__
    text = str(exc)
    if "Authentication" in name or "401" in text or "Unauthorized" in text:
        return "garmin_auth"
    if "TooManyRequests" in name or "429" in text:
        return "garmin_rate_limited"
    return "garmin_error"


def _classify_error_details(details: list[str]) -> str | None:
    joined = " ".join(details)
    if "429" in joined or "Too Many Requests" in joined:
        return "garmin_rate_limited"
    if "401" in joined or "Unauthorized" in joined:
        return "garmin_auth"
    return None


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def run_sync_pipeline(
    db: Database,
    config: HartSettings,
    *,
    health_days: int = 2,
    activity_days: int = 2,
    light: bool = False,
    sync_manager_factory: Callable[[Database, HartSettings], Any] | None = None,
) -> dict[str, Any]:
    """Sync Garmin and update every derived table.

    Parameters
    ----------
    health_days, activity_days:
        How many days back to fetch.  Scheduled runs use 2 (today and
        yesterday, when Garmin finalises a day); manual runs use 7.
    light:
        Morning-watch mode: health for today only, no activities, and only
        the recovery score for today.  The caller escalates to a full run
        once last night's sleep has arrived.
    sync_manager_factory:
        Test hook; defaults to :class:`SyncManager`.
    """
    from hart.analytics.training_load import update_training_load
    from hart.storage.views import refresh_all_views
    from hart.storage.writers import update_sync_state

    if sync_manager_factory is None:
        from hart.ingestion.sync_manager import SyncManager

        sync_manager_factory = SyncManager

    today = datetime.date.today()
    result: dict[str, Any] = {
        "mode": "light" if light else "full",
        "steps": {},
        "errors": [],
        "error_code": None,
        "partial": False,
        "new_activity_ids": [],
        "sleep_today": False,
        "strength_edited_ids": [],
        "effort_edited_ids": [],
    }

    def step(name: str, fn: Callable[[], Any], *, atomic: bool = False) -> Any:
        """Run one step; *atomic* steps rebuild tables the UI reads, so they commit all at once."""
        started = time.monotonic()
        try:
            if atomic:
                with db.transaction():
                    out = fn()
            else:
                out = fn()
            result["steps"][name] = {"ok": True, "ms": int((time.monotonic() - started) * 1000), "out": out}
            return out
        except Exception as exc:  # noqa: BLE001 — every step reports, none aborts the rest
            logger.exception("Pipeline step %s failed", name)
            result["steps"][name] = {"ok": False, "ms": int((time.monotonic() - started) * 1000), "error": str(exc)}
            result["errors"].append(f"{name}: {exc}")
            result["partial"] = True
            return exc

    manager = sync_manager_factory(db, config)
    garmin_configured = bool(config.garmin.email)
    if not garmin_configured:
        # SyncManager silently skips without credentials; make it visible.
        result["error_code"] = "garmin_not_configured"
        result["partial"] = True
        result["errors"].append("GARMIN_EMAIL is not set; Garmin sync skipped")

    # ---- 1. Garmin health ----
    days = 1 if light else health_days
    health = step("garmin_health", lambda: manager.sync_garmin_health(days_back=days))
    if not garmin_configured:
        pass
    elif isinstance(health, Exception):
        result["error_code"] = _classify_garmin_error(health)
    elif health.errors:
        result["partial"] = True
        result["errors"].extend(health.error_details)
        result["error_code"] = _classify_error_details(health.error_details)
        result["steps"]["garmin_health"]["out"] = {"days": health.new_health_days, "errors": health.errors}
    else:
        result["steps"]["garmin_health"]["out"] = {"days": health.new_health_days, "errors": 0}

    touched = [today - datetime.timedelta(days=i) for i in range(days)]
    if isinstance(result["steps"]["garmin_health"].get("out"), dict):
        result["steps"]["garmin_health"]["out"].update(
            {"from": min(touched).isoformat(), "to": max(touched).isoformat()}
        )

    # ---- 2. Garmin activities ----
    if not light and garmin_configured and result["error_code"] != "garmin_auth":
        since = today - datetime.timedelta(days=activity_days + 1)
        before = {r[0] for r in db.fetchall("SELECT activity_id FROM activities WHERE start_time >= ?", [since])}
        acts = step("garmin_activities", lambda: manager.sync_garmin_activities(days_back=activity_days))
        if isinstance(acts, Exception):
            result["error_code"] = result["error_code"] or _classify_garmin_error(acts)
        else:
            if acts.errors:
                result["partial"] = True
                result["errors"].extend(acts.error_details)
                result["error_code"] = result["error_code"] or _classify_error_details(acts.error_details)
            result["steps"]["garmin_activities"]["out"] = {
                "new": acts.new_activities,
                "renamed": acts.updated_activities,
                "errors": acts.errors,
            }
            after = {r[0] for r in db.fetchall("SELECT activity_id FROM activities WHERE start_time >= ?", [since])}
            result["new_activity_ids"] = sorted(after - before)
            # Gym sets are usually corrected in Garmin Connect after the first sync.
            edited = step(
                "strength_edits",
                lambda: manager.refresh_recent_strength_sets(
                    days=activity_days + 1, skip=set(result["new_activity_ids"])
                ),
            )
            if isinstance(edited, list):
                result["strength_edited_ids"] = edited
                result["steps"]["strength_edits"]["out"] = {"updated": len(edited)}
            # RPE and feel are often added or changed in Garmin Connect after the session.
            effort = step(
                "effort_edits",
                lambda: manager.refresh_recent_effort(days=activity_days + 1, skip=set(result["new_activity_ids"])),
            )
            if isinstance(effort, list):
                result["effort_edited_ids"] = effort
                result["steps"]["effort_edits"]["out"] = {"updated": len(effort)}

    # ---- 3-6. Derived tables ----
    if not light:
        step(
            "training_load",
            lambda: update_training_load(
                db,
                ctl_tc=config.analytics.ctl_time_constant,
                atl_tc=config.analytics.atl_time_constant,
            ),
            atomic=True,
        )

    weights = {
        "hrv": config.analytics.recovery_weights.hrv,
        "sleep": config.analytics.recovery_weights.sleep,
        "body_battery": config.analytics.recovery_weights.body_battery,
        "readiness": config.analytics.recovery_weights.readiness,
        "stress": config.analytics.recovery_weights.stress,
        "fatigue": config.analytics.recovery_weights.fatigue,
    }
    empty = db.fetchone("SELECT count(*) FROM daily_recovery")[0] == 0
    if empty and not light:
        # First run after the scores started being persisted: fill history.
        step("recovery_backfill", lambda: {"written": persist_recovery_scores(db, None, weights)}, atomic=True)
    else:
        step("recovery", lambda: {"written": persist_recovery_scores(db, touched, weights)}, atomic=True)

    if not light:
        step("views", lambda: refresh_all_views(db), atomic=True)
        step("anomalies", lambda: insert_new_anomalies(db, lookback_days=14))

    result["sleep_today"] = has_sleep_for(db, today)

    # ---- 7. State ----
    step(
        "sync_state",
        lambda: update_sync_state(
            db,
            "pipeline_light" if light else "pipeline",
            last_sync_at=datetime.datetime.now(tz=datetime.UTC),
            metadata={"partial": result["partial"], "error_code": result["error_code"]},
        ),
    )
    return result
