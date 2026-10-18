"""Grade-adjusted pace (GAP), Normalized Graded Pace and GAP efficiency for outdoor runs.

Computed from the altitude and distance streams (:func:`hart.analytics.pace.grade_adjusted_run`).
Shared by the Garmin sync (new activities), joins and the backfill (schema v20 and the
``decoupling_backfill`` job), so every path computes the same values. Indoor runs (treadmill) and other
sports get none.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from hart.analytics.pace import grade_adjusted_run
from hart.models.activity import StreamPoint
from hart.storage.database import Database

logger = logging.getLogger(__name__)

GAP_COLUMNS = ("gap_pace_sec_km", "ngp_sec_km", "gap_efficiency_factor")


def is_outdoor_run(sport: Any, sub_type: str | None) -> bool:
    from hart.server.data import is_indoor

    return (sport.value if hasattr(sport, "value") else str(sport)) == "run" and not is_indoor(sub_type)


def gap_metrics(
    grade: dict[str, float] | None, avg_pace_sec_km: float | None, efficiency_factor: float | None
) -> dict[str, float | None]:
    """The stored columns: the run's pace and efficiency factor with the hills taken out, and its NGP.

    Grade-adjusting scales the run's own averages by the effort factor, so a flat run keeps its numbers.
    """
    if grade is None:
        return dict.fromkeys(GAP_COLUMNS)
    factor = grade["effort_factor"]
    return {
        "gap_pace_sec_km": round(avg_pace_sec_km / factor, 1) if avg_pace_sec_km else None,
        "ngp_sec_km": round(grade["ngp_sec_km"], 1) if grade.get("ngp_sec_km") else None,
        "gap_efficiency_factor": round(efficiency_factor * factor, 4) if efficiency_factor else None,
    }


def gap_metrics_from_points(
    points: Iterable[StreamPoint], avg_pace_sec_km: float | None, efficiency_factor: float | None
) -> dict[str, float | None]:
    """:func:`gap_metrics` for parsed FIT stream points (the caller checks it is an outdoor run)."""
    pts = sorted(points, key=lambda p: p.timestamp_sec)
    grade = grade_adjusted_run(
        [p.timestamp_sec for p in pts],
        [p.distance for p in pts],
        [p.altitude for p in pts],
        [p.speed for p in pts],
    )
    return gap_metrics(grade, avg_pace_sec_km, efficiency_factor)


def activity_gap_metrics(
    db: Database, activity_id: str, avg_pace_sec_km: float | None, efficiency_factor: float | None
) -> dict[str, float | None]:
    """:func:`gap_metrics` from ``activity_streams`` (the caller checks it is an outdoor run)."""
    rows = db.fetchall(
        "SELECT timestamp_sec, distance, altitude, speed FROM activity_streams WHERE activity_id = ? "
        "AND distance IS NOT NULL AND altitude IS NOT NULL ORDER BY timestamp_sec",
        [activity_id],
    )
    grade = grade_adjusted_run([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows])
    return gap_metrics(grade, avg_pace_sec_km, efficiency_factor)


def backfill_grade_adjusted(db: Database, with_decoupling: bool = True) -> dict[str, int]:
    """Recompute the GAP columns of every outdoor run, and its (grade-adjusted) decoupling unless
    *with_decoupling* is off (the caller just recomputed it).

    Runs without altitude and distance streams get NULLs. Indoor runs and other sports are untouched.
    """
    from hart.ingestion.decoupling import activity_decoupling

    rows = db.fetchall(
        "SELECT m.activity_id, a.sub_type, a.avg_pace_sec_km, m.efficiency_factor FROM activity_metrics m "
        "JOIN activities a USING (activity_id) WHERE a.sport_type = 'run' ORDER BY m.date"
    )
    computed = not_graded = errors = checked = 0
    for activity_id, sub_type, avg_pace, ef in rows:
        if not is_outdoor_run("run", sub_type):
            continue
        checked += 1
        try:
            values: dict[str, float | None] = activity_gap_metrics(db, activity_id, avg_pace, ef)
            if with_decoupling:
                values["aerobic_decoupling_pct"] = activity_decoupling(db, activity_id, "run")
        except Exception as exc:  # one bad stream shouldn't stop the backfill
            logger.warning("Grade-adjusted metrics failed for %s: %s", activity_id, exc)
            errors += 1
            continue
        db.execute(
            f"UPDATE activity_metrics SET {', '.join(f'{c} = ?' for c in values)} WHERE activity_id = ?",
            [*values.values(), activity_id],
        )
        if values["gap_pace_sec_km"] is None and values["gap_efficiency_factor"] is None:
            not_graded += 1
        else:
            computed += 1
    return {"checked": checked, "computed": computed, "no_altitude": not_graded, "errors": errors}
