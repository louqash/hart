"""Computed training metrics, load tracking, weekly summaries, and anomalies."""

from __future__ import annotations

import datetime
import enum

from pydantic import BaseModel

from hart.models.activity import SportType


class ActivityMetrics(BaseModel):
    """Derived metrics computed from a single activity."""

    activity_id: str
    sport_type: SportType
    date: datetime.date
    tss: float | None = None
    tss_method: str | None = None
    hr_zone_seconds: dict[str, int] = {}
    efficiency_factor: float | None = None
    aerobic_decoupling_pct: float | None = None
    avg_ground_contact_ms: float | None = None
    avg_vertical_osc_mm: float | None = None
    avg_vertical_ratio_pct: float | None = None
    avg_stride_length_m: float | None = None
    swolf: float | None = None
    estimated_calories: int | None = None
    estimated_kj: float | None = None
    carb_calories: float | None = None
    fat_calories: float | None = None


class TrainingLoadDay(BaseModel):
    """Daily training-load bookkeeping (PMC model)."""

    date: datetime.date
    sport_type: str
    daily_tss: float
    ctl: float  # chronic training load (fitness)
    atl: float  # acute training load (fatigue)
    tsb: float  # training stress balance (form)
    monotony: float | None = None
    strain: float | None = None


class WeeklySummary(BaseModel):
    """Aggregated weekly training summary per sport."""

    week_start: datetime.date
    sport_type: SportType
    session_count: int = 0
    total_duration_sec: int = 0
    total_distance_m: float = 0.0
    total_tss: float = 0.0
    total_elevation_m: float = 0.0
    avg_hr: float | None = None
    avg_ef: float | None = None
    avg_decoupling: float | None = None
    hr_zone_seconds: dict[str, int] = {}
    longest_session_sec: int = 0


class RecoveryScore(BaseModel):
    """Composite daily recovery score (0-100)."""

    date: datetime.date
    recovery_score: float  # 0-100
    hrv_component: float | None = None
    sleep_component: float | None = None
    body_battery_component: float | None = None
    readiness_component: float | None = None
    stress_component: float | None = None
    fatigue_component: float | None = None
    notes: str | None = None


class AnomalySeverity(str, enum.Enum):
    """How serious an anomaly is."""

    info = "info"
    warning = "warning"
    critical = "critical"


class Anomaly(BaseModel):
    """A detected anomaly in training or health data."""

    id: str
    detected_at: datetime.datetime
    anomaly_type: str
    severity: AnomalySeverity
    sport_type: SportType | None = None
    metric_name: str
    expected_value: float | None = None
    actual_value: float | None = None
    z_score: float | None = None
    description: str
    activity_id: str | None = None
    date_range_start: datetime.date | None = None
    date_range_end: datetime.date | None = None
    acknowledged: bool = False
