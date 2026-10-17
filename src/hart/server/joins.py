"""Sessions recorded in parts: joining them when they sync, by hand, and splitting them back.

The data work is in :mod:`hart.ingestion.joins` (and ``SyncManager.split_back``); this module adds what the
server knows about: the gap setting, the note requests waiting on Discord, what to grade next, and the
derived tables to bring up to date after a join or split made outside a sync.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Collection
from typing import Any

from hart.analytics.joins import end_time, join_chains
from hart.config import HartSettings
from hart.ingestion import joins
from hart.server import grade_wait, settings
from hart.server.data import rows
from hart.storage.database import Database

logger = logging.getLogger(__name__)

# The session page offers to join a recording of the same sport that ended at most this long before the other
# one started (a long café stop still counts; a morning and an evening run don't).
OFFER_MAX_GAP_MIN = 180
# What a note request on Discord says once its recording was joined into the session before it.
JOINED = "🔗 Joined with the rest of the session — it's graded as one."


def join(db: Database, config: HartSettings | None, activity_ids: list[str], method: str) -> dict[str, Any]:
    """Join the activities into the earliest one (:class:`hart.ingestion.joins.JoinError` if they can't be).

    Besides what :func:`hart.ingestion.joins.join_activities` returns: ``waited`` — a later part was waiting
    for its note (that request is closed; :func:`grade_after` asks again if the session still needs one)."""
    parts = joins.load_parts(db, activity_ids)
    waited = [p["activity_id"] for p in parts[1:] if grade_wait.release(db, config, p["activity_id"], JOINED)]
    out = joins.join_activities(db, [p["activity_id"] for p in parts], method)
    out["waited"] = bool(waited)
    return out


def auto_join(db: Database, config: HartSettings | None, since: datetime.date) -> list[dict[str, Any]]:
    """Join recordings since *since* that look like one session (``join_gap_minutes``). Recordings split back
    by hand are left alone."""
    gap = int(settings.get(db, "join_gap_minutes") or 0)
    if gap <= 0:
        return []
    candidates = rows(
        db,
        "SELECT activity_id, sport_type, sub_type, start_time, elapsed_seconds FROM activities "
        "WHERE start_time >= ? AND activity_id NOT IN "
        "(SELECT activity_id FROM activity_joins WHERE joined_into IS NULL) ORDER BY start_time",
        [since],
    )
    joined = []
    for chain in join_chains(candidates, gap):
        try:
            joined.append(join(db, config, chain, "auto"))
        except joins.JoinError as exc:  # noqa: PERF203 — rare, and one chain shouldn't stop the others
            logger.warning("Auto-join of %s skipped: %s", chain, exc)
    return joined


def grade_after(
    db: Database,
    config: HartSettings | None,
    joined: dict[str, Any],
    enqueue: Any,
    new_ids: Collection[str] = (),
) -> str | None:
    """Grade the joined session like a new one, unless the sync already does (it's new) or it waits for its note:
    without a real grade yet — or when a part's note request was closed — it waits for the note if one is
    missing, else it's graded now; with one, it's graded again."""
    activity_id = joined["activity_id"]
    if activity_id in new_ids or grade_wait.is_waiting(db, activity_id):
        return None
    graded = (
        db.fetchone("SELECT 1 FROM session_grades WHERE activity_id = ? AND status = 'graded'", [activity_id])
        is not None
    )
    if (not graded or joined.get("waited")) and config is not None and grade_wait.hold(db, config, [activity_id]):
        return "waiting"
    grade_wait.grade(enqueue, activity_id, "joined", job_trigger="chain")
    return "grading"


def refresh_derived(db: Database, config: HartSettings, dates: list[datetime.date]) -> None:
    """After a join or split outside a sync: load, the summary views and plan matching for the days touched."""
    from hart.analytics.training_load import update_training_load
    from hart.server.data import local_today
    from hart.server.plan import auto_match
    from hart.storage.views import refresh_all_views

    auto_match(db, dates)
    with db.transaction():
        update_training_load(
            db,
            ctl_tc=config.analytics.ctl_time_constant,
            atl_tc=config.analytics.atl_time_constant,
            until=local_today(config),
        )
    with db.transaction():
        refresh_all_views(db)


# ---------------------------------------------------------------------------
# The session page
# ---------------------------------------------------------------------------


def session_info(db: Database, activity_id: str) -> dict[str, Any]:
    """For the session page: the recordings it was joined from, and the recordings of the same sport just
    before and after it the same day (to join by hand)."""
    me = rows(
        db,
        "SELECT activity_id, sport_type, start_time, elapsed_seconds, source FROM activities WHERE activity_id = ?",
        [activity_id],
    )
    if not me:
        return {}
    a = me[0]
    parts = joins.joined_parts(db, activity_id)
    neighbours = rows(
        db,
        "SELECT activity_id, name, start_time, elapsed_seconds, coalesce(moving_seconds, elapsed_seconds) AS seconds "
        "FROM activities WHERE sport_type = ? AND CAST(start_time AS DATE) = ? AND activity_id <> ? "
        "ORDER BY start_time",
        [a["sport_type"], a["start_time"].date(), activity_id],
    )
    before = [n for n in neighbours if n["start_time"] < a["start_time"]]
    after = [n for n in neighbours if n["start_time"] > a["start_time"]]

    def offer(n: dict[str, Any], earlier: dict[str, Any], later: dict[str, Any]) -> dict[str, Any] | None:
        gap = (later["start_time"] - end_time(earlier)).total_seconds()
        if not 0 <= gap <= OFFER_MAX_GAP_MIN * 60:
            return None  # overlapping, or hours apart: not one session stopped and started again
        return {
            "activity_id": n["activity_id"],
            "name": n["name"],
            "start_time": n["start_time"],
            "seconds": n["seconds"],
            "gap_min": round(gap / 60),
        }

    return {
        "part_starts": [a["start_time"], *(p["start_time"] for p in parts)] if parts else [],
        "auto": any(p["method"] == "auto" for p in parts),
        "can_split": bool(parts) and a["source"] == "garmin",
        "join_before": offer(before[-1], before[-1], a) if before else None,
        "join_after": offer(after[0], a, after[0]) if after else None,
    }
