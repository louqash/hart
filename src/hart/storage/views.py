"""Materialised-view management for pre-aggregated analytics tables.

DuckDB does not support native materialised views, so we emulate them with
ordinary tables that are dropped and recreated on demand (the ``mv_*`` naming
convention keeps them distinct from source tables).

Call :func:`refresh_all_views` after a data import or on a schedule to keep
the pre-aggregated data current.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hart.storage.database import Database

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Individual view refreshers
# ---------------------------------------------------------------------------


def refresh_training_load_90d(db: Database) -> None:
    """Rebuild ``mv_training_load_90d`` with the last 90 days of PMC data.

    Includes daily TSS, CTL, ATL, TSB, and rolling 7-day monotony/strain
    for every sport type tracked.
    """
    logger.info("Refreshing mv_training_load_90d")
    db.execute("DROP TABLE IF EXISTS mv_training_load_90d")
    db.execute("""
        CREATE TABLE mv_training_load_90d AS
        SELECT
            tl.date,
            tl.sport_type,
            tl.daily_tss,
            tl.ctl,
            tl.atl,
            tl.tsb,
            tl.monotony,
            tl.strain,
            -- 7-day rolling average TSS for quick trend lines
            AVG(tl.daily_tss) OVER (
                PARTITION BY tl.sport_type
                ORDER BY tl.date
                ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
            ) AS tss_7d_avg,
            -- Running count of training days in the 7-day window
            COUNT(CASE WHEN tl.daily_tss > 0 THEN 1 END) OVER (
                PARTITION BY tl.sport_type
                ORDER BY tl.date
                ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
            ) AS training_days_7d
        FROM daily_training_load tl
        WHERE tl.date >= CURRENT_DATE - INTERVAL 90 DAY
        ORDER BY tl.date, tl.sport_type
    """)


def refresh_current_week(db: Database) -> None:
    """Rebuild ``mv_current_week`` with this ISO week's summary so far.

    Joins weekly_summary with daily activity counts for a live snapshot.
    """
    logger.info("Refreshing mv_current_week")
    db.execute("DROP TABLE IF EXISTS mv_current_week")
    db.execute("""
        CREATE TABLE mv_current_week AS
        WITH week_bounds AS (
            SELECT
                DATE_TRUNC('week', CURRENT_DATE)::DATE AS week_start,
                (DATE_TRUNC('week', CURRENT_DATE) + INTERVAL 6 DAY)::DATE AS week_end
        ),
        daily_stats AS (
            SELECT
                a.sport_type,
                CAST(a.start_time AS DATE) AS activity_date,
                COUNT(*) AS sessions,
                SUM(a.elapsed_seconds) AS total_sec,
                SUM(a.distance_meters) AS total_dist,
                SUM(a.total_elevation_m) AS total_elev,
                AVG(a.avg_hr) AS avg_hr
            FROM activities a, week_bounds wb
            WHERE CAST(a.start_time AS DATE) >= wb.week_start
              AND CAST(a.start_time AS DATE) <= wb.week_end
            GROUP BY a.sport_type, CAST(a.start_time AS DATE)
        )
        SELECT
            wb.week_start,
            ds.sport_type,
            ds.activity_date,
            ds.sessions,
            ds.total_sec,
            ds.total_dist,
            ds.total_elev,
            ds.avg_hr,
            -- Cumulative within the week
            SUM(ds.total_sec) OVER (
                PARTITION BY ds.sport_type
                ORDER BY ds.activity_date
            ) AS cum_duration_sec,
            SUM(ds.total_dist) OVER (
                PARTITION BY ds.sport_type
                ORDER BY ds.activity_date
            ) AS cum_distance_m
        FROM daily_stats ds, week_bounds wb
        ORDER BY ds.sport_type, ds.activity_date
    """)


def refresh_rolling_metrics(db: Database) -> None:
    """Rebuild ``mv_rolling_metrics_28d`` with 28-day rolling averages.

    Computes rolling averages for key performance indicators:
    efficiency factor, aerobic decoupling, TSS, HR, and duration per
    sport type.
    """
    logger.info("Refreshing mv_rolling_metrics_28d")
    db.execute("DROP TABLE IF EXISTS mv_rolling_metrics_28d")
    db.execute("""
        CREATE TABLE mv_rolling_metrics_28d AS
        WITH daily_agg AS (
            SELECT
                m.date,
                m.sport_type,
                COUNT(*) AS session_count,
                AVG(m.tss) AS avg_tss,
                AVG(m.efficiency_factor) AS avg_ef,
                AVG(m.aerobic_decoupling_pct) AS avg_decoupling,
                AVG(a.avg_hr) AS avg_hr,
                AVG(a.elapsed_seconds) AS avg_duration_sec,
                AVG(a.distance_meters) AS avg_distance_m
            FROM activity_metrics m
            JOIN activities a ON m.activity_id = a.activity_id
            WHERE m.date >= CURRENT_DATE - INTERVAL 56 DAY
            GROUP BY m.date, m.sport_type
        )
        SELECT
            d.date,
            d.sport_type,
            d.session_count,
            d.avg_tss,
            d.avg_ef,
            d.avg_decoupling,
            d.avg_hr,
            d.avg_duration_sec,
            d.avg_distance_m,
            -- 28-day rolling averages
            AVG(d.avg_tss) OVER w AS rolling_28d_tss,
            AVG(d.avg_ef) OVER w AS rolling_28d_ef,
            AVG(d.avg_decoupling) OVER w AS rolling_28d_decoupling,
            AVG(d.avg_hr) OVER w AS rolling_28d_hr,
            AVG(d.avg_duration_sec) OVER w AS rolling_28d_duration_sec,
            SUM(d.session_count) OVER w AS rolling_28d_sessions
        FROM daily_agg d
        WHERE d.date >= CURRENT_DATE - INTERVAL 28 DAY
        WINDOW w AS (
            PARTITION BY d.sport_type
            ORDER BY d.date
            ROWS BETWEEN 27 PRECEDING AND CURRENT ROW
        )
        ORDER BY d.date, d.sport_type
    """)


# ---------------------------------------------------------------------------
# Aggregate refresher
# ---------------------------------------------------------------------------


def refresh_all_views(db: Database) -> None:
    """Drop and recreate all ``mv_*`` materialised-view tables.

    Call this after a batch import or on a scheduled cadence to keep
    pre-aggregated analytics current.
    """
    logger.info("Refreshing all materialised views")
    refresh_training_load_90d(db)
    refresh_current_week(db)
    refresh_rolling_metrics(db)
    logger.info("All materialised views refreshed")
