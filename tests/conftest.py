"""Shared test fixtures."""

import json
from datetime import date, datetime, timedelta

import pytest


@pytest.fixture
def sample_activity():
    """A sample bike activity dict."""
    return {
        "activity_id": "test_bike_001",
        "source": "test",
        "external_id": "001",
        "sport_type": "bike",
        "sub_type": "road_cycling",
        "name": "Morning Ride",
        "description": None,
        "start_time": datetime(2026, 4, 1, 7, 0, 0),
        "elapsed_seconds": 3600,
        "moving_seconds": 3500,
        "distance_meters": 30000,
        "total_elevation_m": 200,
        "avg_hr": 145.0,
        "max_hr": 172.0,
        "avg_power": 200.0,
        "max_power": 450.0,
        "normalized_power": 215.0,
        "avg_cadence": 88.0,
        "avg_pace_sec_km": None,
        "avg_speed_kmh": 30.0,
        "calories": 800,
        "avg_temperature": 18.0,
        "training_effect_aerobic": 3.5,
        "training_effect_anaerobic": 1.2,
        "fit_file_path": None,
        "gear_id": None,
        "weather_summary": None,
        "imported_at": datetime.now(),
    }


@pytest.fixture
def sample_run_activity():
    """A sample run activity dict."""
    return {
        "activity_id": "test_run_001",
        "source": "test",
        "external_id": "002",
        "sport_type": "run",
        "sub_type": "road_running",
        "name": "Easy Run",
        "start_time": datetime(2026, 4, 1, 17, 0, 0),
        "elapsed_seconds": 2700,
        "moving_seconds": 2650,
        "distance_meters": 8000,
        "total_elevation_m": 50,
        "avg_hr": 150.0,
        "max_hr": 165.0,
        "avg_power": None,
        "max_power": None,
        "normalized_power": None,
        "avg_cadence": 175.0,
        "avg_pace_sec_km": 337.5,  # 5:37/km
        "avg_speed_kmh": 10.67,
        "calories": 550,
        "avg_temperature": 20.0,
        "training_effect_aerobic": 3.0,
        "training_effect_anaerobic": 0.5,
        "fit_file_path": None,
        "gear_id": None,
        "weather_summary": None,
        "imported_at": datetime.now(),
    }


@pytest.fixture
def sample_power_series():
    """1-hour bike power data with realistic variation."""
    import random

    random.seed(42)
    base_power = 200
    return [max(0, base_power + random.gauss(0, 30)) for _ in range(3600)]


@pytest.fixture
def sample_pace_series():
    """45-min run pace data in sec/km."""
    import random

    random.seed(42)
    base_pace = 330  # 5:30/km
    return [max(200, base_pace + random.gauss(0, 15)) for _ in range(2700)]


@pytest.fixture
def sample_grade_series():
    """Corresponding grade data for pace series."""
    import math

    return [math.sin(i / 200) * 5 for i in range(2700)]  # oscillating ±5%


@pytest.fixture
def athlete_config():
    """Sample athlete configuration."""
    return {
        "ftp": 250,
        "lthr": 170,
        "max_hr": 195,
        "resting_hr": 48,
        "functional_threshold_pace_sec_km": 300,  # 5:00/km
        "css_sec_100m": 105,  # 1:45/100m
        "weight_kg": 75,
    }


@pytest.fixture
def sample_daily_tss():
    """30 days of daily TSS values."""
    import random

    random.seed(42)
    tss_values = []
    base_date = date(2026, 3, 1)
    for i in range(30):
        d = base_date + timedelta(days=i)
        # Simulate: some days off, varied load
        if i % 7 == 6:  # rest day
            tss = 0
        elif i % 7 == 3:  # hard day
            tss = random.uniform(80, 120)
        else:
            tss = random.uniform(40, 80)
        tss_values.append((d, tss))
    return tss_values


def write_seed(seed_dir, races=None):
    """A seed directory with one A-race (the app ships no personal defaults)."""

    seed_dir.mkdir(parents=True, exist_ok=True)
    races = (
        races
        if races is not None
        else [{"name": "Example Ironman", "race_date": "2027-08-22", "distance": "full", "priority": "A"}]
    )
    (seed_dir / "races.json").write_text(json.dumps(races), encoding="utf-8")
    return seed_dir
