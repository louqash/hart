"""hart storage layer — DuckDB-backed persistence.

Quick start::

    from hart.storage import Database, get_database

    db = get_database("data/hart.duckdb")
    # ... use writers / queries ...
"""

from hart.storage.database import Database, get_database
from hart.storage.queries import (
    get_activities,
    get_activity_by_id,
    get_activity_laps,
    get_activity_metrics,
    get_activity_streams,
    get_anomalies,
    get_athlete_profile,
    get_daily_health,
    get_hrv_trend,
    get_latest_activity_time,
    get_recovery_scores,
    get_sleep_data,
    get_strength_sets,
    get_sync_state,
    get_training_load,
    get_weekly_summaries,
    run_analytics_query,
)
from hart.storage.views import (
    refresh_all_views,
    refresh_current_week,
    refresh_rolling_metrics,
    refresh_training_load_90d,
)
from hart.storage.writers import (
    insert_anomaly,
    replace_strength_sets,
    update_sync_state,
    upsert_activities,
    upsert_activity,
    upsert_activity_metrics,
    upsert_health_day,
    upsert_hrv_daily,
    upsert_hrv_samples,
    upsert_laps,
    upsert_recovery_score,
    upsert_sleep_record,
    upsert_stream_points,
    upsert_training_load_day,
    upsert_weekly_summary,
)

__all__ = [
    # Core
    "Database",
    "get_database",
    # Writers
    "insert_anomaly",
    "replace_strength_sets",
    "update_sync_state",
    "upsert_activities",
    "upsert_activity",
    "upsert_activity_metrics",
    "upsert_health_day",
    "upsert_hrv_daily",
    "upsert_hrv_samples",
    "upsert_laps",
    "upsert_recovery_score",
    "upsert_sleep_record",
    "upsert_stream_points",
    "upsert_training_load_day",
    "upsert_weekly_summary",
    # Queries
    "get_activities",
    "get_activity_by_id",
    "get_activity_laps",
    "get_activity_metrics",
    "get_activity_streams",
    "get_anomalies",
    "get_athlete_profile",
    "get_daily_health",
    "get_hrv_trend",
    "get_latest_activity_time",
    "get_recovery_scores",
    "get_sleep_data",
    "get_strength_sets",
    "get_sync_state",
    "get_training_load",
    "get_weekly_summaries",
    "run_analytics_query",
    # Views
    "refresh_all_views",
    "refresh_current_week",
    "refresh_rolling_metrics",
    "refresh_training_load_90d",
]
