"""Parameterised read queries for the hart database.

Every public function accepts a :class:`~hart.storage.database.Database`
and optional filter parameters, returning plain ``dict`` rows (or specialised
types where noted).
"""

from __future__ import annotations

import datetime
import re
from typing import Any

from hart.storage.database import Database

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _rows_to_dicts(
    db: Database,
    sql: str,
    params: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Execute *sql* and return results as a list of dictionaries."""
    df = db.fetchdf(sql, params)
    return df.to_dict(orient="records")  # type: ignore[return-value]


def _build_where(
    clauses: list[str],
    params: list[Any],
) -> str:
    """Join non-empty WHERE clauses with AND."""
    if not clauses:
        return ""
    return " WHERE " + " AND ".join(clauses)


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


def get_activities(
    db: Database,
    *,
    sport_type: str | None = None,
    start_date: datetime.date | None = None,
    end_date: datetime.date | None = None,
    limit: int = 50,
    sort_by: str = "date",
) -> list[dict[str, Any]]:
    """Return activities matching the given filters.

    Parameters
    ----------
    sort_by:
        ``"date"`` (default, descending), ``"distance"``, ``"duration"``, or
        ``"tss"``.
    """
    clauses: list[str] = []
    params: list[Any] = []

    if sport_type is not None:
        clauses.append("a.sport_type = ?")
        params.append(sport_type)
    if start_date is not None:
        clauses.append("a.start_time >= ?")
        params.append(start_date)
    if end_date is not None:
        clauses.append("a.start_time < ? + INTERVAL 1 DAY")
        params.append(end_date)

    order_map: dict[str, str] = {
        "date": "a.start_time DESC",
        "distance": "a.distance_meters DESC NULLS LAST",
        "duration": "a.elapsed_seconds DESC",
        "tss": "m.tss DESC NULLS LAST",
    }
    order = order_map.get(sort_by, "a.start_time DESC")

    sql = (
        "SELECT a.*, m.tss, m.efficiency_factor "
        "FROM activities a "
        "LEFT JOIN activity_metrics m ON a.activity_id = m.activity_id"
        + _build_where(clauses, params)
        + f" ORDER BY {order} LIMIT ?"
    )
    params.append(limit)
    return _rows_to_dicts(db, sql, params)


def get_activity_by_id(
    db: Database,
    activity_id: str,
) -> dict[str, Any] | None:
    """Return a single activity with its metrics, or ``None``."""
    rows = _rows_to_dicts(
        db,
        "SELECT a.*, m.tss, m.tss_method, "
        "m.efficiency_factor, m.aerobic_decoupling_pct, m.hr_zone_seconds "
        "FROM activities a "
        "LEFT JOIN activity_metrics m ON a.activity_id = m.activity_id "
        "WHERE a.activity_id = ?",
        [activity_id],
    )
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Streams & laps
# ---------------------------------------------------------------------------


def get_activity_streams(
    db: Database,
    activity_id: str,
) -> list[dict[str, Any]]:
    """Return all stream points for an activity, ordered by timestamp."""
    return _rows_to_dicts(
        db,
        "SELECT * FROM activity_streams "
        "WHERE activity_id = ? ORDER BY timestamp_sec",
        [activity_id],
    )


def get_strength_sets(
    db: Database,
    activity_id: str,
) -> list[dict[str, Any]]:
    """Return strength sets for an activity, ordered by index."""
    return _rows_to_dicts(
        db,
        "SELECT * FROM strength_sets "
        "WHERE activity_id = ? ORDER BY set_index",
        [activity_id],
    )


def get_activity_laps(
    db: Database,
    activity_id: str,
) -> list[dict[str, Any]]:
    """Return laps for an activity, ordered by index."""
    return _rows_to_dicts(
        db,
        "SELECT * FROM activity_laps "
        "WHERE activity_id = ? ORDER BY lap_index",
        [activity_id],
    )


# ---------------------------------------------------------------------------
# Training load / PMC
# ---------------------------------------------------------------------------


def get_training_load(
    db: Database,
    start_date: datetime.date,
    end_date: datetime.date,
    sport_type: str = "combined",
) -> list[dict[str, Any]]:
    """Return daily training-load rows between *start_date* and *end_date*."""
    return _rows_to_dicts(
        db,
        "SELECT * FROM daily_training_load "
        "WHERE date >= ? AND date <= ? AND sport_type = ? "
        "ORDER BY date",
        [start_date, end_date, sport_type],
    )


# ---------------------------------------------------------------------------
# Health, sleep, HRV
# ---------------------------------------------------------------------------


def get_daily_health(
    db: Database,
    start_date: datetime.date,
    end_date: datetime.date,
) -> list[dict[str, Any]]:
    """Return daily health rows for the given date range."""
    return _rows_to_dicts(
        db,
        "SELECT * FROM daily_health "
        "WHERE date >= ? AND date <= ? ORDER BY date",
        [start_date, end_date],
    )


def get_sleep_data(
    db: Database,
    start_date: datetime.date,
    end_date: datetime.date,
) -> list[dict[str, Any]]:
    """Return sleep records for the given date range."""
    return _rows_to_dicts(
        db,
        "SELECT * FROM sleep_records "
        "WHERE date >= ? AND date <= ? ORDER BY date",
        [start_date, end_date],
    )


def get_hrv_trend(
    db: Database,
    start_date: datetime.date,
    end_date: datetime.date,
) -> list[dict[str, Any]]:
    """Return daily HRV data for the given date range."""
    return _rows_to_dicts(
        db,
        "SELECT * FROM hrv_daily "
        "WHERE date >= ? AND date <= ? ORDER BY date",
        [start_date, end_date],
    )


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def get_recovery_scores(
    db: Database,
    start_date: datetime.date,
    end_date: datetime.date,
) -> list[dict[str, Any]]:
    """Return recovery scores for the given date range."""
    return _rows_to_dicts(
        db,
        "SELECT * FROM daily_recovery "
        "WHERE date >= ? AND date <= ? ORDER BY date",
        [start_date, end_date],
    )


# ---------------------------------------------------------------------------
# Activity metrics
# ---------------------------------------------------------------------------


def get_activity_metrics(
    db: Database,
    *,
    activity_ids: list[str] | None = None,
    sport_type: str | None = None,
    start_date: datetime.date | None = None,
    end_date: datetime.date | None = None,
) -> list[dict[str, Any]]:
    """Return computed activity metrics matching the given filters."""
    clauses: list[str] = []
    params: list[Any] = []

    if activity_ids is not None:
        placeholders = ", ".join("?" for _ in activity_ids)
        clauses.append(f"activity_id IN ({placeholders})")
        params.extend(activity_ids)
    if sport_type is not None:
        clauses.append("sport_type = ?")
        params.append(sport_type)
    if start_date is not None:
        clauses.append("date >= ?")
        params.append(start_date)
    if end_date is not None:
        clauses.append("date <= ?")
        params.append(end_date)

    sql = "SELECT * FROM activity_metrics" + _build_where(clauses, params) + " ORDER BY date DESC"
    return _rows_to_dicts(db, sql, params)


# ---------------------------------------------------------------------------
# Weekly summaries
# ---------------------------------------------------------------------------


def get_weekly_summaries(
    db: Database,
    start_date: datetime.date,
    end_date: datetime.date,
    sport_type: str | None = None,
) -> list[dict[str, Any]]:
    """Return weekly summaries for the given date range."""
    clauses: list[str] = ["week_start >= ?", "week_start <= ?"]
    params: list[Any] = [start_date, end_date]

    if sport_type is not None:
        clauses.append("sport_type = ?")
        params.append(sport_type)

    sql = "SELECT * FROM weekly_summary" + _build_where(clauses, params) + " ORDER BY week_start"
    return _rows_to_dicts(db, sql, params)


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------


def get_anomalies(
    db: Database,
    *,
    start_date: datetime.date | None = None,
    end_date: datetime.date | None = None,
    severity: str | None = None,
    anomaly_type: str | None = None,
) -> list[dict[str, Any]]:
    """Return anomaly log entries matching the given filters."""
    clauses: list[str] = []
    params: list[Any] = []

    if start_date is not None:
        clauses.append("detected_at >= ?")
        params.append(start_date)
    if end_date is not None:
        clauses.append("detected_at < ? + INTERVAL 1 DAY")
        params.append(end_date)
    if severity is not None:
        clauses.append("severity = ?")
        params.append(severity)
    if anomaly_type is not None:
        clauses.append("anomaly_type = ?")
        params.append(anomaly_type)

    sql = (
        "SELECT * FROM anomaly_log"
        + _build_where(clauses, params)
        + " ORDER BY detected_at DESC"
    )
    return _rows_to_dicts(db, sql, params)


# ---------------------------------------------------------------------------
# Athlete profile (composite read)
# ---------------------------------------------------------------------------


def get_athlete_profile(db: Database) -> dict[str, Any]:
    """Build an athlete profile summary from the latest available data.

    Reads the most-recent VO2max values from ``daily_health`` and latest
    training-load snapshot.
    """
    profile: dict[str, Any] = {}

    # Latest VO2max values
    row = db.fetchone(
        "SELECT vo2max_run, vo2max_cycle FROM daily_health "
        "WHERE vo2max_run IS NOT NULL OR vo2max_cycle IS NOT NULL "
        "ORDER BY date DESC LIMIT 1",
    )
    if row:
        profile["vo2max_run"] = row[0]
        profile["vo2max_cycle"] = row[1]

    # Latest resting HR
    row = db.fetchone(
        "SELECT resting_hr FROM daily_health "
        "WHERE resting_hr IS NOT NULL ORDER BY date DESC LIMIT 1",
    )
    if row:
        profile["resting_hr"] = row[0]

    # Latest CTL/ATL/TSB per sport
    load_rows = _rows_to_dicts(
        db,
        "SELECT sport_type, ctl, atl, tsb FROM daily_training_load "
        "WHERE (date, sport_type) IN ("
        "  SELECT MAX(date), sport_type FROM daily_training_load "
        "  GROUP BY sport_type"
        ")",
    )
    profile["training_load"] = load_rows

    # Latest recovery score
    row = db.fetchone(
        "SELECT recovery_score, date FROM daily_recovery "
        "ORDER BY date DESC LIMIT 1",
    )
    if row:
        profile["latest_recovery_score"] = row[0]
        profile["latest_recovery_date"] = row[1]

    return profile


# ---------------------------------------------------------------------------
# Sync state
# ---------------------------------------------------------------------------


def get_sync_state(
    db: Database,
    source: str,
) -> dict[str, Any] | None:
    """Return the sync state for a data source, or ``None``."""
    rows = _rows_to_dicts(
        db,
        "SELECT * FROM sync_state WHERE source = ?",
        [source],
    )
    return rows[0] if rows else None


def get_latest_activity_time(
    db: Database,
    source: str | None = None,
) -> datetime.datetime | None:
    """Return the timestamp of the most recent activity.

    If *source* is given, filter to activities from that source.
    """
    if source is not None:
        row = db.fetchone(
            "SELECT MAX(start_time) FROM activities WHERE source = ?",
            [source],
        )
    else:
        row = db.fetchone("SELECT MAX(start_time) FROM activities")

    if row and row[0] is not None:
        val = row[0]
        if isinstance(val, datetime.datetime):
            return val
        # DuckDB may return a string in some edge cases.
        return datetime.datetime.fromisoformat(str(val))
    return None


# ---------------------------------------------------------------------------
# Arbitrary analytics query (read-only)
# ---------------------------------------------------------------------------

_FORBIDDEN_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|MERGE|REPLACE|COPY|ATTACH|DETACH|EXPORT|IMPORT|LOAD|INSTALL)\b",
    re.IGNORECASE,
)


# Table functions that only generate values; everything else (read_csv,
# read_text, glob, parquet_scan, pragma_*, …) can touch the filesystem or
# engine settings and is rejected.
_ALLOWED_TABLE_FUNCTIONS = frozenset({"range", "generate_series", "unnest"})


def _check_read_only_select(db: Database, sql: str) -> None:
    """Reject anything but a single SELECT over database tables.

    Keyword filtering alone is not enough: a plain ``SELECT`` can read files
    via table functions (``read_text('/proc/self/environ')``) or replacement
    scans (``SELECT * FROM '/etc/passwd'``).  The query is parsed with
    DuckDB's own parser and its syntax tree is inspected.
    """
    import json

    import duckdb

    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as exc:
        raise ValueError(f"Could not parse query: {exc}") from exc
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        raise ValueError("Only a single SELECT statement is allowed.")

    row = db.fetchone("SELECT json_serialize_sql(?)", [sql])
    tree = json.loads(row[0]) if row else {"error": True}
    if tree.get("error"):
        raise ValueError(f"Could not parse query: {tree.get('error_message', '')}")

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            node_type = node.get("type")
            if node_type == "TABLE_FUNCTION":
                name = str(node.get("function", {}).get("function_name", "")).lower()
                if name not in _ALLOWED_TABLE_FUNCTIONS:
                    raise ValueError(f"Table function '{name}' is not allowed.")
            elif node_type == "BASE_TABLE":
                name = str(node.get("table_name", ""))
                if any(ch in name for ch in "/\\.:") or name.lower().startswith("http"):
                    raise ValueError(f"Reading '{name}' is not allowed; query tables only.")
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(tree.get("statements", []))


def run_analytics_query(
    db: Database,
    sql: str,
    *,
    max_rows: int = 100,
) -> list[dict[str, Any]]:
    """Execute an arbitrary **read-only** SQL query and return up to *max_rows*.

    Raises :class:`ValueError` if the query contains write/DDL keywords.
    """
    stripped = sql.strip().rstrip(";")
    if _FORBIDDEN_PATTERN.search(stripped):
        raise ValueError(
            "Only SELECT queries are allowed.  "
            "Detected a write/DDL keyword in the query."
        )
    _check_read_only_select(db, stripped)

    # Force a row limit to prevent runaway queries.
    sql_limited = f"SELECT * FROM ({stripped}) AS _q LIMIT {max_rows}"
    return _rows_to_dicts(db, sql_limited)
