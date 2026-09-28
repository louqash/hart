"""Upsert / bulk-insert functions for every hart table.

All writers accept a :class:`~hart.storage.database.Database` instance and
one or more Pydantic model objects.  Writes use ``INSERT OR REPLACE`` (which
DuckDB supports) so that re-importing the same data is idempotent.

For high-volume stream data the functions delete-then-bulk-insert for maximum
throughput.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from hart.models import (
    Activity,
    ActivityMetrics,
    Anomaly,
    HealthDay,
    HRVDaily,
    Lap,
    RecoveryScore,
    SleepRecord,
    StreamPoint,
    StrengthSet,
    TrainingLoadDay,
    WeeklySummary,
)
from hart.storage.database import Database

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_or_none(value: dict[str, Any] | list[Any] | None) -> str | None:
    """Serialise a dict/list to a JSON string, or return ``None``."""
    if value is None:
        return None
    return json.dumps(value)


def _val(model: Any, field: str) -> Any:
    """Extract a field from a Pydantic model, converting enums to their value."""
    v = getattr(model, field)
    if v is None:
        return None
    if hasattr(v, "value"):
        return v.value
    return v


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------

_ACTIVITY_COLS: list[str] = [
    "activity_id",
    "source",
    "external_id",
    "sport_type",
    "sub_type",
    "name",
    "description",
    "start_time",
    "elapsed_seconds",
    "moving_seconds",
    "distance_meters",
    "total_elevation_m",
    "avg_hr",
    "max_hr",
    "avg_power",
    "max_power",
    "normalized_power",
    "avg_cadence",
    "avg_pace_sec_km",
    "avg_speed_kmh",
    "calories",
    "avg_temperature",
    "training_effect_aerobic",
    "training_effect_anaerobic",
    "fit_file_path",
    "gear_id",
    "weather_summary",
    "rpe",
    "feel",
    "garmin_training_load",
    "training_effect_label",
    "body_battery_delta",
    "begin_stamina",
    "end_stamina",
    "imported_at",
]


def _activity_values(a: Activity) -> list[Any]:
    return [
        a.activity_id,
        a.source,
        a.external_id,
        _val(a, "sport_type"),
        a.sub_type,
        a.name,
        a.description,
        a.start_time,
        a.elapsed_seconds,
        a.moving_seconds,
        a.distance_meters,
        a.total_elevation_m,
        a.avg_hr,
        a.max_hr,
        a.avg_power,
        a.max_power,
        a.normalized_power,
        a.avg_cadence,
        a.avg_pace_sec_km,
        a.avg_speed_kmh,
        a.calories,
        a.avg_temperature,
        a.training_effect_aerobic,
        a.training_effect_anaerobic,
        a.fit_file_path,
        a.gear_id,
        a.weather_summary,
        a.rpe,
        a.feel,
        a.garmin_training_load,
        a.training_effect_label,
        a.body_battery_delta,
        a.begin_stamina,
        a.end_stamina,
        a.imported_at or datetime.utcnow(),
    ]


_ACTIVITY_SQL: str = (
    "INSERT INTO activities ("
    + ", ".join(_ACTIVITY_COLS)
    + ") VALUES ("
    + ", ".join("?" for _ in _ACTIVITY_COLS)
    + ") ON CONFLICT (activity_id) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _ACTIVITY_COLS if c != "activity_id")
)


def upsert_activity(db: Database, activity: Activity) -> None:
    """Insert or replace a single activity."""
    db.execute(_ACTIVITY_SQL, _activity_values(activity))


def upsert_activities(db: Database, activities: list[Activity]) -> None:
    """Bulk upsert a list of activities."""
    if not activities:
        return
    db.executemany(_ACTIVITY_SQL, [_activity_values(a) for a in activities])


# ---------------------------------------------------------------------------
# Activity streams
# ---------------------------------------------------------------------------

_STREAM_COLS: list[str] = [
    "activity_id",
    "timestamp_sec",
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
]

_STREAM_SQL: str = (
    "INSERT INTO activity_streams ("
    + ", ".join(_STREAM_COLS)
    + ") VALUES ("
    + ", ".join("?" for _ in _STREAM_COLS)
    + ")"
)


def _stream_row(activity_id: str, sp: StreamPoint) -> list[Any]:
    return [
        activity_id,
        int(sp.timestamp_sec),
        sp.heart_rate,
        sp.power,
        sp.cadence,
        sp.speed,
        sp.altitude,
        sp.distance,
        sp.latitude,
        sp.longitude,
        sp.temperature,
        sp.grade_percent,
        int(sp.ground_contact_time_ms) if sp.ground_contact_time_ms is not None else None,
        int(sp.vertical_oscillation_mm) if sp.vertical_oscillation_mm is not None else None,
        sp.vertical_ratio_pct,
        sp.stride_length_m,
        sp.respiration_rate,
    ]


def upsert_stream_points(
    db: Database,
    activity_id: str,
    points: list[StreamPoint],
) -> None:
    """Replace all stream data for *activity_id* with *points*.

    Deletes existing rows first for a clean swap, then bulk-inserts via
    ``executemany`` for optimal throughput.
    """
    db.execute("DELETE FROM activity_streams WHERE activity_id = ?", [activity_id])
    if not points:
        return
    db.executemany(_STREAM_SQL, [_stream_row(activity_id, p) for p in points])


# ---------------------------------------------------------------------------
# Laps
# ---------------------------------------------------------------------------

_LAP_COLS: list[str] = [
    "activity_id",
    "lap_index",
    "start_time",
    "elapsed_seconds",
    "moving_seconds",
    "distance_meters",
    "avg_hr",
    "max_hr",
    "avg_power",
    "avg_cadence",
    "avg_pace_sec_km",
    "total_elevation_m",
    "avg_temperature",
]

_LAP_SQL: str = (
    "INSERT OR REPLACE INTO activity_laps ("
    + ", ".join(_LAP_COLS)
    + ") VALUES ("
    + ", ".join("?" for _ in _LAP_COLS)
    + ")"
)


def _lap_row(activity_id: str, lap: Lap) -> list[Any]:
    return [
        activity_id,
        lap.lap_index,
        lap.start_time,
        lap.elapsed_seconds,
        lap.moving_seconds,
        lap.distance_meters,
        lap.avg_hr,
        lap.max_hr,
        lap.avg_power,
        lap.avg_cadence,
        lap.avg_pace_sec_km,
        lap.total_elevation_m,
        lap.avg_temperature,
    ]


def upsert_laps(db: Database, activity_id: str, laps: list[Lap]) -> None:
    """Insert or replace laps for *activity_id*."""
    if not laps:
        return
    db.executemany(_LAP_SQL, [_lap_row(activity_id, lap) for lap in laps])


# ---------------------------------------------------------------------------
# Strength sets
# ---------------------------------------------------------------------------

_STRENGTH_SET_COLS: list[str] = [
    "activity_id",
    "set_index",
    "set_type",
    "start_time",
    "duration_sec",
    "repetitions",
    "weight_kg",
    "exercise_category",
    "exercise_name",
    "exercise_confidence",
]

_STRENGTH_SET_SQL: str = (
    f"INSERT INTO strength_sets ({', '.join(_STRENGTH_SET_COLS)}) VALUES ({', '.join('?' for _ in _STRENGTH_SET_COLS)})"
)


def replace_strength_sets(db: Database, activity_id: str, sets: list[StrengthSet]) -> None:
    """Replace all strength sets for *activity_id*.

    Delete-then-insert because set counts change when the athlete edits a
    workout in Garmin Connect.  No-op for an empty list so a FIT-only
    re-import never wipes richer API data.
    """
    if not sets:
        return
    db.execute("DELETE FROM strength_sets WHERE activity_id = ?", [activity_id])
    db.executemany(
        _STRENGTH_SET_SQL,
        [
            [
                activity_id,
                s.set_index,
                s.set_type,
                s.start_time,
                s.duration_sec,
                s.repetitions,
                s.weight_kg,
                s.exercise_category,
                s.exercise_name,
                s.exercise_confidence,
            ]
            for s in sets
        ],
    )


# ---------------------------------------------------------------------------
# HRV samples
# ---------------------------------------------------------------------------

_HRV_SAMPLE_SQL: str = "INSERT OR REPLACE INTO hrv_samples (activity_id, sample_index, rr_interval_ms) VALUES (?, ?, ?)"


def upsert_hrv_samples(
    db: Database,
    activity_id: str,
    samples: list[tuple[int, float]],
) -> None:
    """Insert or replace HRV R-R interval samples.

    Parameters
    ----------
    samples:
        A list of ``(sample_index, rr_interval_ms)`` tuples.
    """
    if not samples:
        return
    db.executemany(
        _HRV_SAMPLE_SQL,
        [[activity_id, idx, rr_ms] for idx, rr_ms in samples],
    )


# ---------------------------------------------------------------------------
# Daily health
# ---------------------------------------------------------------------------


def _merge_upsert_sql(table: str, cols: list[str], key: str = "date") -> str:
    """Upsert that never erases data: a NULL from the source keeps the stored value.

    Garmin days are re-fetched on every sync (they're finalised late); if one of
    the several API calls for a day fails, its fields arrive as NULL and must
    not overwrite values fetched earlier.
    """
    updates = ", ".join(f"{c} = COALESCE(EXCLUDED.{c}, {table}.{c})" for c in cols if c != key)
    return (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
        f"ON CONFLICT ({key}) DO UPDATE SET {updates}"
    )


_HEALTH_COLS: list[str] = [
    "date",
    "resting_hr",
    "min_hr",
    "max_hr_day",
    "hr_p25",
    "hr_p50",
    "hr_p75",
    "avg_stress",
    "max_stress",
    "stress_p50",
    "stress_p75",
    "stress_p90",
    "stress_p95",
    "high_stress_duration",
    "medium_stress_duration",
    "low_stress_duration",
    "rest_stress_duration",
    "body_battery_high",
    "body_battery_low",
    "body_battery_start",
    "training_readiness",
    "vo2max_run",
    "vo2max_cycle",
    "respiration_avg",
    "respiration_min",
    "respiration_max",
    "steps",
    "active_calories",
    "total_calories",
]

_HEALTH_SQL: str = _merge_upsert_sql("daily_health", _HEALTH_COLS)


def upsert_health_day(db: Database, health: HealthDay) -> None:
    """Insert or replace a daily health record."""
    db.execute(
        _HEALTH_SQL,
        [
            health.date,
            health.resting_hr,
            health.min_hr,
            health.max_hr_day,
            health.hr_p25,
            health.hr_p50,
            health.hr_p75,
            health.avg_stress,
            health.max_stress,
            health.stress_p50,
            health.stress_p75,
            health.stress_p90,
            health.stress_p95,
            health.high_stress_duration,
            health.medium_stress_duration,
            health.low_stress_duration,
            health.rest_stress_duration,
            health.body_battery_high,
            health.body_battery_low,
            health.body_battery_start,
            health.training_readiness,
            health.vo2max_run,
            health.vo2max_cycle,
            health.respiration_avg,
            health.respiration_min,
            health.respiration_max,
            health.steps,
            health.active_calories,
            health.total_calories,
        ],
    )


# ---------------------------------------------------------------------------
# Sleep records
# ---------------------------------------------------------------------------

_SLEEP_COLS: list[str] = [
    "date",
    "sleep_start",
    "sleep_end",
    "total_sleep_sec",
    "deep_sleep_sec",
    "light_sleep_sec",
    "rem_sleep_sec",
    "awake_sec",
    "sleep_score",
    "avg_spo2",
    "avg_respiration",
    "avg_hr_sleep",
    "hrv_status",
    "hrv_overnight_ms",
]

_SLEEP_SQL: str = _merge_upsert_sql("sleep_records", _SLEEP_COLS)


def upsert_sleep_record(db: Database, sleep: SleepRecord) -> None:
    """Insert or replace a sleep record."""
    db.execute(
        _SLEEP_SQL,
        [
            sleep.date,
            sleep.sleep_start,
            sleep.sleep_end,
            sleep.total_sleep_sec,
            sleep.deep_sleep_sec,
            sleep.light_sleep_sec,
            sleep.rem_sleep_sec,
            sleep.awake_sec,
            sleep.sleep_score,
            sleep.avg_spo2,
            sleep.avg_respiration,
            sleep.avg_hr_sleep,
            sleep.hrv_status,
            sleep.hrv_overnight_ms,
        ],
    )


# ---------------------------------------------------------------------------
# HRV daily
# ---------------------------------------------------------------------------

_HRV_DAILY_COLS: list[str] = [
    "date",
    "hrv_weekly_avg_ms",
    "hrv_last_night_ms",
    "hrv_last_night_5min_high",
    "hrv_status",
    "baseline_low_ms",
    "baseline_high_ms",
]

_HRV_DAILY_SQL: str = _merge_upsert_sql("hrv_daily", _HRV_DAILY_COLS)


def upsert_hrv_daily(db: Database, hrv: HRVDaily) -> None:
    """Insert or replace a daily HRV summary."""
    db.execute(
        _HRV_DAILY_SQL,
        [
            hrv.date,
            hrv.hrv_weekly_avg_ms,
            hrv.hrv_last_night_ms,
            hrv.hrv_last_night_5min_high,
            hrv.hrv_status,
            hrv.baseline_low_ms,
            hrv.baseline_high_ms,
        ],
    )


# ---------------------------------------------------------------------------
# Lactate tests
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Activity metrics (computed)
# ---------------------------------------------------------------------------

_METRICS_COLS: list[str] = [
    "activity_id",
    "sport_type",
    "date",
    "tss",
    "tss_method",
    "hr_zone_seconds",
    "efficiency_factor",
    "aerobic_decoupling_pct",
    "avg_ground_contact_ms",
    "avg_vertical_osc_mm",
    "avg_vertical_ratio_pct",
    "avg_stride_length_m",
    "swolf",
    "estimated_calories",
    "estimated_kj",
    "carb_calories",
    "fat_calories",
]

_METRICS_SQL: str = (
    "INSERT OR REPLACE INTO activity_metrics ("
    + ", ".join(_METRICS_COLS)
    + ") VALUES ("
    + ", ".join("?" for _ in _METRICS_COLS)
    + ")"
)


def upsert_activity_metrics(db: Database, metrics: ActivityMetrics) -> None:
    """Insert or replace computed activity metrics."""
    db.execute(
        _METRICS_SQL,
        [
            metrics.activity_id,
            _val(metrics, "sport_type"),
            metrics.date,
            metrics.tss,
            metrics.tss_method,
            _json_or_none(metrics.hr_zone_seconds),
            metrics.efficiency_factor,
            metrics.aerobic_decoupling_pct,
            metrics.avg_ground_contact_ms,
            metrics.avg_vertical_osc_mm,
            metrics.avg_vertical_ratio_pct,
            metrics.avg_stride_length_m,
            metrics.swolf,
            metrics.estimated_calories,
            metrics.estimated_kj,
            metrics.carb_calories,
            metrics.fat_calories,
        ],
    )


# ---------------------------------------------------------------------------
# Daily training load
# ---------------------------------------------------------------------------

_TL_COLS: list[str] = [
    "date",
    "sport_type",
    "daily_tss",
    "ctl",
    "atl",
    "tsb",
    "monotony",
    "strain",
]

_TL_SQL: str = (
    "INSERT OR REPLACE INTO daily_training_load ("
    + ", ".join(_TL_COLS)
    + ") VALUES ("
    + ", ".join("?" for _ in _TL_COLS)
    + ")"
)


def upsert_training_load_day(db: Database, tl: TrainingLoadDay) -> None:
    """Insert or replace a daily training-load record."""
    db.execute(
        _TL_SQL,
        [
            tl.date,
            tl.sport_type,
            tl.daily_tss,
            tl.ctl,
            tl.atl,
            tl.tsb,
            tl.monotony,
            tl.strain,
        ],
    )


# ---------------------------------------------------------------------------
# Weekly summary
# ---------------------------------------------------------------------------

_WS_COLS: list[str] = [
    "week_start",
    "sport_type",
    "session_count",
    "total_duration_sec",
    "total_distance_m",
    "total_tss",
    "total_elevation_m",
    "avg_hr",
    "avg_ef",
    "avg_decoupling",
    "hr_zone_seconds",
    "longest_session_sec",
]

_WS_SQL: str = (
    "INSERT OR REPLACE INTO weekly_summary ("
    + ", ".join(_WS_COLS)
    + ") VALUES ("
    + ", ".join("?" for _ in _WS_COLS)
    + ")"
)


def upsert_weekly_summary(db: Database, ws: WeeklySummary) -> None:
    """Insert or replace a weekly summary record."""
    db.execute(
        _WS_SQL,
        [
            ws.week_start,
            _val(ws, "sport_type"),
            ws.session_count,
            ws.total_duration_sec,
            ws.total_distance_m,
            ws.total_tss,
            ws.total_elevation_m,
            ws.avg_hr,
            ws.avg_ef,
            ws.avg_decoupling,
            _json_or_none(ws.hr_zone_seconds),
            ws.longest_session_sec,
        ],
    )


# ---------------------------------------------------------------------------
# Recovery score
# ---------------------------------------------------------------------------

_RS_COLS: list[str] = [
    "date",
    "recovery_score",
    "hrv_component",
    "sleep_component",
    "body_battery_component",
    "readiness_component",
    "stress_component",
    "fatigue_component",
    "notes",
]

_RS_SQL: str = (
    "INSERT OR REPLACE INTO daily_recovery ("
    + ", ".join(_RS_COLS)
    + ") VALUES ("
    + ", ".join("?" for _ in _RS_COLS)
    + ")"
)


def upsert_recovery_score(db: Database, rs: RecoveryScore) -> None:
    """Insert or replace a daily recovery score."""
    db.execute(
        _RS_SQL,
        [
            rs.date,
            rs.recovery_score,
            rs.hrv_component,
            rs.sleep_component,
            rs.body_battery_component,
            rs.readiness_component,
            rs.stress_component,
            rs.fatigue_component,
            rs.notes,
        ],
    )


# ---------------------------------------------------------------------------
# Anomaly log
# ---------------------------------------------------------------------------

_ANOMALY_INSERT_COLS: list[str] = [
    "anomaly_type",
    "severity",
    "sport_type",
    "metric_name",
    "expected_value",
    "actual_value",
    "z_score",
    "description",
    "activity_id",
    "date_range_start",
    "date_range_end",
    "acknowledged",
]

_ANOMALY_SQL: str = (
    "INSERT INTO anomaly_log ("
    + ", ".join(_ANOMALY_INSERT_COLS)
    + ") VALUES ("
    + ", ".join("?" for _ in _ANOMALY_INSERT_COLS)
    + ")"
)


def insert_anomaly(db: Database, anomaly: Anomaly) -> int:
    """Insert an anomaly and return its auto-generated id."""
    db.execute(
        _ANOMALY_SQL,
        [
            anomaly.anomaly_type,
            _val(anomaly, "severity"),
            _val(anomaly, "sport_type"),
            anomaly.metric_name,
            anomaly.expected_value,
            anomaly.actual_value,
            anomaly.z_score,
            anomaly.description,
            anomaly.activity_id,
            anomaly.date_range_start,
            anomaly.date_range_end,
            anomaly.acknowledged,
        ],
    )
    row = db.fetchone("SELECT currval('anomaly_id_seq')")
    assert row is not None, "Failed to retrieve anomaly id from sequence"
    return int(row[0])


# ---------------------------------------------------------------------------
# Sync state
# ---------------------------------------------------------------------------


def update_sync_state(
    db: Database,
    source: str,
    last_sync_at: datetime | None = None,
    last_activity_time: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Insert or replace sync state for a data source."""
    db.execute(
        "INSERT OR REPLACE INTO sync_state (source, last_sync_at, last_activity_time, metadata) VALUES (?, ?, ?, ?)",
        [source, last_sync_at, last_activity_time, _json_or_none(metadata)],
    )
