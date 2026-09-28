"""Health, sleep, HRV, and lactate-test models."""

from __future__ import annotations

import datetime

from pydantic import BaseModel



class HealthDay(BaseModel):
    """Aggregated daily health metrics (primarily from Garmin)."""

    date: datetime.date
    resting_hr: int | None = None
    min_hr: int | None = None
    max_hr_day: int | None = None
    # HR percentiles from non-activity 2-min time-series
    hr_p25: float | None = None
    hr_p50: float | None = None
    hr_p75: float | None = None
    # Stress: avg/max from API, percentiles from 3-min time-series
    avg_stress: float | None = None
    max_stress: float | None = None
    stress_p50: float | None = None
    stress_p75: float | None = None
    stress_p90: float | None = None
    stress_p95: float | None = None
    # Stress duration breakdown (seconds)
    high_stress_duration: int | None = None
    medium_stress_duration: int | None = None
    low_stress_duration: int | None = None
    rest_stress_duration: int | None = None
    # Body battery
    body_battery_high: int | None = None
    body_battery_low: int | None = None
    body_battery_start: int | None = None
    training_readiness: int | None = None
    vo2max_run: float | None = None
    vo2max_cycle: float | None = None
    respiration_avg: float | None = None
    respiration_min: float | None = None
    respiration_max: float | None = None
    steps: int | None = None
    active_calories: int | None = None
    total_calories: int | None = None


class SleepRecord(BaseModel):
    """Nightly sleep data."""

    date: datetime.date
    sleep_start: datetime.datetime | None = None
    sleep_end: datetime.datetime | None = None
    total_sleep_sec: int | None = None
    deep_sleep_sec: int | None = None
    light_sleep_sec: int | None = None
    rem_sleep_sec: int | None = None
    awake_sec: int | None = None
    sleep_score: int | None = None
    avg_spo2: float | None = None
    avg_respiration: float | None = None
    avg_hr_sleep: int | None = None
    hrv_status: str | None = None
    hrv_overnight_ms: float | None = None


class HRVDaily(BaseModel):
    """Daily HRV summary."""

    date: datetime.date
    hrv_weekly_avg_ms: float | None = None
    hrv_last_night_ms: float | None = None
    hrv_last_night_5min_high: float | None = None
    hrv_status: str | None = None
    baseline_low_ms: float | None = None
    baseline_high_ms: float | None = None


