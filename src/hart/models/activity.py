"""Activity, stream-point, and lap models."""

from __future__ import annotations

import datetime
import enum

from pydantic import BaseModel


class SportType(str, enum.Enum):
    """Supported sport types."""

    swim = "swim"
    bike = "bike"
    run = "run"
    strength = "strength"
    other = "other"


class Activity(BaseModel):
    """Full activity record persisted in the database."""

    activity_id: str
    source: str  # e.g. "strava", "garmin", "manual"
    external_id: str | None = None
    sport_type: SportType
    sub_type: str | None = None
    name: str | None = None
    description: str | None = None
    start_time: datetime.datetime
    elapsed_seconds: int
    moving_seconds: int | None = None
    distance_meters: float | None = None
    total_elevation_m: float | None = None
    avg_hr: int | None = None
    max_hr: int | None = None
    avg_power: int | None = None
    max_power: int | None = None
    normalized_power: int | None = None
    avg_cadence: int | None = None
    avg_pace_sec_km: float | None = None
    avg_speed_kmh: float | None = None
    calories: int | None = None
    avg_temperature: float | None = None
    training_effect_aerobic: float | None = None
    training_effect_anaerobic: float | None = None
    fit_file_path: str | None = None
    gear_id: str | None = None
    weather_summary: str | None = None
    # Garmin summaryDTO fields (not in .fit files)
    rpe: int | None = None  # directWorkoutRpe (0-100)
    feel: int | None = None  # directWorkoutFeel (0-100)
    garmin_training_load: float | None = None
    training_effect_label: str | None = None
    body_battery_delta: int | None = None
    begin_stamina: float | None = None
    end_stamina: float | None = None
    imported_at: datetime.datetime | None = None


class StreamPoint(BaseModel):
    """Single time-series data sample from a recorded activity."""

    timestamp_sec: float
    heart_rate: int | None = None
    power: int | None = None
    cadence: int | None = None
    speed: float | None = None
    altitude: float | None = None
    distance: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    temperature: float | None = None
    grade_percent: float | None = None
    ground_contact_time_ms: float | None = None
    vertical_oscillation_mm: float | None = None
    vertical_ratio_pct: float | None = None
    stride_length_m: float | None = None
    respiration_rate: float | None = None


class Lap(BaseModel):
    """Lap / split data for an activity."""

    lap_index: int
    start_time: datetime.datetime | None = None
    elapsed_seconds: int
    moving_seconds: int | None = None
    distance_meters: float | None = None
    avg_hr: int | None = None
    max_hr: int | None = None
    avg_power: int | None = None
    avg_cadence: int | None = None
    avg_pace_sec_km: float | None = None
    total_elevation_m: float | None = None
    avg_temperature: float | None = None
    # Structured workouts: the lap's kind (warmup, active, interval, rest, recovery, cooldown) and the
    # workout step it belongs to (WorkoutStep.step_index).
    intensity: str | None = None
    wkt_step_index: int | None = None


class WorkoutStep(BaseModel):
    """One step of the structured workout that was followed (from the FIT file)."""

    step_index: int
    name: str | None = None
    intensity: str | None = None  # warmup, active, interval, rest, recovery, cooldown
    duration_type: str | None = None  # time, distance, open, repeat_until_steps_cmplt, …
    duration_value: float | None = None  # seconds or metres
    target_type: str | None = None  # power, heart_rate, speed, cadence, open
    target_low: float | None = None
    target_high: float | None = None
    target_unit: str | None = None  # W, %FTP, bpm, %HRmax, sec_km, rpm, zone
    repeat_from: int | None = None  # repeat steps: back to this step…
    repeat_count: int | None = None  # …this many times
    notes: str | None = None


class StrengthSet(BaseModel):
    """One set (active or rest) from a strength-training activity."""

    set_index: int
    set_type: str  # "active" or "rest"
    start_time: datetime.datetime | None = None
    duration_sec: float | None = None
    repetitions: int | None = None
    weight_kg: float | None = None  # 0 = bodyweight, None = not recorded
    exercise_category: str | None = None  # e.g. "DEADLIFT"
    exercise_name: str | None = None  # e.g. "BARBELL_DEADLIFT"
    exercise_confidence: float | None = None  # Garmin auto-detect probability (0-100)
