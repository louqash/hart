"""Aerobic decoupling from stored or freshly parsed activity streams.

Shared by the Garmin sync (new activities), the ``backfill_decoupling`` server
job, ``hart sync backfill-decoupling`` and ``backfill-metrics``, so every path
applies the same gates (see :func:`steady_session_decoupling`).
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from hart.analytics.efficiency import steady_session_decoupling
from hart.models.activity import StreamPoint
from hart.storage.database import Database

logger = logging.getLogger(__name__)

# Bike decoupling is Pw:Hr (outdoor rides without a power meter get none);
# run decoupling is Pa:Hr from speed.
_OUTPUT_FIELD = {"bike": "power", "run": "speed"}


def _sport_str(sport: Any) -> str:
    return sport.value if hasattr(sport, "value") else str(sport)


def decoupling_from_points(points: Iterable[StreamPoint], sport: Any) -> float | None:
    """Decoupling (%) for parsed FIT stream points, or ``None`` if ineligible."""
    field = _OUTPUT_FIELD.get(_sport_str(sport))
    if field is None:
        return None
    pts = list(points)
    return _rounded(steady_session_decoupling(
        [p.timestamp_sec for p in pts],
        [p.heart_rate for p in pts],
        [getattr(p, field) for p in pts],
        _sport_str(sport),
    ))


def activity_decoupling(db: Database, activity_id: str, sport: Any) -> float | None:
    """Decoupling (%) for an activity from ``activity_streams``, or ``None``."""
    field = _OUTPUT_FIELD.get(_sport_str(sport))
    if field is None:
        return None
    rows = db.fetchall(
        f"SELECT timestamp_sec, heart_rate, {field} FROM activity_streams "
        "WHERE activity_id = ? ORDER BY timestamp_sec",
        [activity_id],
    )
    if not rows:
        return None
    return _rounded(steady_session_decoupling(
        [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], _sport_str(sport),
    ))


def backfill_decoupling(db: Database, only_missing: bool = False) -> dict[str, int]:
    """Recompute ``activity_metrics.aerobic_decoupling_pct`` for bike and run.

    Only that column is updated (other metrics are untouched); ineligible
    sessions are set to NULL so a tightened gate also clears old values.
    """
    sql = (
        "SELECT m.activity_id, m.sport_type FROM activity_metrics m "
        "WHERE m.sport_type IN ('bike', 'run')"
    )
    if only_missing:
        sql += " AND m.aerobic_decoupling_pct IS NULL"
    rows = db.fetchall(sql + " ORDER BY m.date")

    computed = cleared = errors = 0
    for activity_id, sport in rows:
        try:
            value = activity_decoupling(db, activity_id, sport)
        except Exception as exc:  # one bad stream shouldn't stop the backfill
            logger.warning("Decoupling failed for %s: %s", activity_id, exc)
            errors += 1
            continue
        db.execute(
            "UPDATE activity_metrics SET aerobic_decoupling_pct = ? WHERE activity_id = ?",
            [value, activity_id],
        )
        if value is None:
            cleared += 1
        else:
            computed += 1
    return {"checked": len(rows), "computed": computed, "not_eligible": cleared, "errors": errors}


def _rounded(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None
