"""Aerobic decoupling: gated computation, sync write, backfill and server job."""

from __future__ import annotations

import dataclasses
import datetime
import time
from pathlib import Path
from typing import Any

import pytest

from hart.analytics.efficiency import steady_session_decoupling
from hart.config import ServerSettings, get_config
from hart.ingestion.decoupling import (
    activity_decoupling,
    backfill_decoupling,
    decoupling_from_points,
)
from hart.ingestion.sync_manager import SyncManager
from hart.models.activity import Activity, SportType, StreamPoint
from hart.storage.database import Database

EPOCH = 1_790_422_870  # activity_streams stores absolute epoch seconds
WARMUP = 600


def _steady(
    minutes: int,
    *,
    output: float = 200.0,
    hr_start: float = 130.0,
    hr_end: float = 130.0,
    t0: float = EPOCH,
) -> tuple[list[float], list[float | None], list[float | None]]:
    """1 Hz steady stream whose HR rises linearly from hr_start to hr_end."""
    n = minutes * 60
    ts = [t0 + i for i in range(n)]
    hr = [hr_start + (hr_end - hr_start) * i / (n - 1) for i in range(n)]
    return ts, hr, [output] * n


def _expected(hr: list[float], output: float) -> float:
    """Decoupling of a constant-output stream after the warm-up, split by time."""
    analysed = hr[WARMUP:]
    mid = len(analysed) // 2
    first = sum(analysed[:mid]) / mid
    second = sum(analysed[mid:]) / (len(analysed) - mid)
    ef1, ef2 = output / first, output / second
    return (ef1 - ef2) / ef1 * 100


# ---------------------------------------------------------------------------
# steady_session_decoupling
# ---------------------------------------------------------------------------


class TestSteadySessionDecoupling:
    def test_steady_ride_with_hr_drift(self) -> None:
        ts, hr, pw = _steady(60, hr_start=125, hr_end=140)
        value = steady_session_decoupling(ts, hr, pw, "bike")
        assert value is not None and value > 0
        assert value == pytest.approx(_expected(hr, 200.0), abs=0.05)

    def test_epoch_and_relative_timestamps_agree(self) -> None:
        ts, hr, pw = _steady(60, hr_start=125, hr_end=140)
        relative = [t - EPOCH for t in ts]
        assert steady_session_decoupling(ts, hr, pw, "bike") == pytest.approx(
            steady_session_decoupling(relative, hr, pw, "bike")
        )

    def test_no_drift_is_zero(self) -> None:
        ts, hr, pw = _steady(50)
        assert steady_session_decoupling(ts, hr, pw, "bike") == pytest.approx(0.0)

    def test_shorter_than_45_minutes_is_none(self) -> None:
        ts, hr, pw = _steady(44, hr_start=125, hr_end=140)
        assert steady_session_decoupling(ts, hr, pw, "bike") is None

    def test_pauses_do_not_count_as_moving_time(self) -> None:
        # 40 moving minutes with a 30-minute café stop in the middle.
        ts, hr, pw = _steady(40)
        ts = ts[:1200] + [t + 1800 for t in ts[1200:]]
        assert steady_session_decoupling(ts, hr, pw, "bike") is None

    def test_warmup_is_excluded(self) -> None:
        ts, hr, pw = _steady(60)
        # Warm-up at very low power would dominate the first half otherwise.
        pw = [100.0] * WARMUP + pw[WARMUP:]
        assert steady_session_decoupling(ts, hr, pw, "bike") == pytest.approx(0.0)

    def test_interval_ride_is_not_steady(self) -> None:
        ts, hr, _ = _steady(60)
        # 3 min at 320 W / 3 min at 120 W: VI well above 1.10.
        pw = [320.0 if (i // 180) % 2 == 0 else 120.0 for i in range(len(ts))]
        assert steady_session_decoupling(ts, hr, pw, "bike") is None
        assert steady_session_decoupling(ts, hr, pw, "bike", require_steady=False) is not None

    def test_ride_without_power_is_none(self) -> None:
        ts, hr, _ = _steady(90)
        assert steady_session_decoupling(ts, hr, [None] * len(ts), "bike") is None

    def test_missing_samples_stay_aligned(self) -> None:
        # HR dropouts must not shift the power series (the old backfill bug).
        ts, hr, pw = _steady(60, hr_start=125, hr_end=140)
        gappy_hr = [None if i % 7 == 0 else h for i, h in enumerate(hr)]
        value = steady_session_decoupling(ts, gappy_hr, pw, "bike")
        assert value == pytest.approx(_expected(hr, 200.0), abs=0.1)

    def test_steady_run_uses_speed(self) -> None:
        # Speed in km/h as stored; slowing down at the same HR = positive decoupling.
        n = 50 * 60
        ts = [EPOCH + i for i in range(n)]
        hr = [150.0] * n
        speed = [10.0 - 0.5 * i / (n - 1) for i in range(n)]
        value = steady_session_decoupling(ts, hr, speed, "run")
        assert value is not None and 1.0 < value < 5.0

    def test_interval_run_is_not_steady(self) -> None:
        n = 60 * 60
        ts = [EPOCH + i for i in range(n)]
        speed = [14.0 if (i // 120) % 2 == 0 else 8.0 for i in range(n)]
        assert steady_session_decoupling(ts, [150.0] * n, speed, "run") is None

    def test_other_sports_and_bad_input(self) -> None:
        ts, hr, pw = _steady(60)
        assert steady_session_decoupling(ts, hr, pw, "swim") is None
        assert steady_session_decoupling(ts, hr[:-1], pw, "bike") is None
        assert steady_session_decoupling([], [], [], "bike") is None


# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.duckdb").connect()
    yield database
    database.close()


def _insert_activity(
    db: Database, act_id: str, sport: str, streams: tuple[list, list, list] | None, ef: float = 1.5
) -> None:
    db.execute(
        "INSERT INTO activities (activity_id, source, sport_type, start_time, elapsed_seconds) "
        "VALUES (?, 'test', ?, TIMESTAMP '2026-09-20 08:00:00', 3600)",
        [act_id, sport],
    )
    db.execute(
        "INSERT INTO activity_metrics (activity_id, sport_type, date, tss, efficiency_factor) "
        "VALUES (?, ?, DATE '2026-09-20', 55, ?)",
        [act_id, sport, ef],
    )
    if streams:
        field = "power" if sport == "bike" else "speed"
        ts, hr, out = streams
        db.connection.executemany(
            f"INSERT INTO activity_streams (activity_id, timestamp_sec, heart_rate, {field}) "
            "VALUES (?, ?, ?, ?)",
            [[act_id, int(t), None if h is None else round(h), o] for t, h, o in zip(ts, hr, out)],
        )


def _stored(db: Database, act_id: str) -> tuple[Any, ...]:
    return db.fetchone(
        "SELECT aerobic_decoupling_pct, efficiency_factor, tss FROM activity_metrics "
        "WHERE activity_id = ?",
        [act_id],
    )


def test_backfill_fills_only_eligible_sessions(db: Database) -> None:
    _insert_activity(db, "steady_ride", "bike", _steady(60, hr_start=125, hr_end=140))
    _insert_activity(db, "short_ride", "bike", _steady(30))
    _insert_activity(db, "no_streams_run", "run", None)
    _insert_activity(db, "swim", "swim", None)

    result = backfill_decoupling(db)

    assert result == {"checked": 3, "computed": 1, "not_eligible": 2, "errors": 0}
    value, ef, tss = _stored(db, "steady_ride")
    assert value is not None and value > 0
    assert (ef, tss) == (1.5, 55)  # other metrics untouched
    assert _stored(db, "short_ride")[0] is None
    assert activity_decoupling(db, "steady_ride", "bike") == value


def test_backfill_only_missing_keeps_existing_values(db: Database) -> None:
    _insert_activity(db, "ride", "bike", _steady(60, hr_start=125, hr_end=140))
    db.execute("UPDATE activity_metrics SET aerobic_decoupling_pct = 99 WHERE activity_id = 'ride'")

    assert backfill_decoupling(db, only_missing=True)["checked"] == 0
    assert _stored(db, "ride")[0] == 99
    backfill_decoupling(db)
    assert _stored(db, "ride")[0] != 99


def test_decoupling_from_parsed_points() -> None:
    ts, hr, pw = _steady(60, hr_start=125, hr_end=140)
    points = [StreamPoint(timestamp_sec=t, heart_rate=round(h), power=int(p)) for t, h, p in zip(ts, hr, pw)]
    assert decoupling_from_points(points, SportType.bike) > 0
    assert decoupling_from_points(points, SportType.swim) is None


class _MetricsGarmin:
    def get_activity_hr_in_timezones(self, ext_id: str) -> list[dict[str, Any]]:
        return [{"zoneNumber": 2, "secsInZone": 3600.0}]


def test_sync_writes_decoupling_for_new_activity(db: Database) -> None:
    ts, hr, pw = _steady(60, hr_start=125, hr_end=140)
    points = [StreamPoint(timestamp_sec=t, heart_rate=round(h), power=int(p)) for t, h, p in zip(ts, hr, pw)]
    activity = Activity(
        activity_id="fit_ride", source="garmin", sport_type=SportType.bike,
        start_time=datetime.datetime(2026, 9, 26, 13, 41), elapsed_seconds=3600,
    )
    db.execute(
        "INSERT INTO activities (activity_id, source, sport_type, start_time, elapsed_seconds) "
        "VALUES ('fit_ride', 'garmin', 'bike', TIMESTAMP '2026-09-26 13:41:00', 3600)"
    )
    summary = {"normalizedPower": 200, "averageHR": 132}

    SyncManager(db, get_config())._sync_activity_metrics(_MetricsGarmin(), "1", activity, summary, points)

    value, ef, _ = _stored(db, "fit_ride")
    assert value == pytest.approx(decoupling_from_points(points, SportType.bike))
    assert value > 0 and ef == pytest.approx(200 / 132, abs=1e-4)


def test_backfill_job_via_server(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from hart.server.app import create_app, job_details

    db_path = tmp_path / "app.duckdb"
    db = Database(db_path).connect()
    _insert_activity(db, "steady_ride", "bike", _steady(60, hr_start=125, hr_end=140))
    _insert_activity(db, "short_run", "run", None)
    db.close()

    config = dataclasses.replace(
        get_config(),
        db_path=db_path,
        garmin_token_path=tmp_path / "tokens",
        server=ServerSettings(env="production", seed_dir=tmp_path / "seed"),
    )
    # Default handlers: the real decoupling_backfill job runs.
    app = create_app(config, run_scheduler=False)
    headers = {"Tailscale-User-Login": "athlete@example.com", "X-Requested-With": "hart"}
    with TestClient(app, base_url="https://hart.example.ts.net") as client:
        job = client.post("/api/backfill/decoupling", json={}, headers=headers).json()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = client.get(f"/api/jobs/{job['job_id']}", headers=headers).json()
            if status["status"] not in ("queued", "running"):
                break
            time.sleep(0.05)

    assert status["status"] == "ok", status
    assert status["result"] == {"checked": 2, "computed": 1, "not_eligible": 1, "errors": 0}
    assert job_details(status) == "decoupling computed for 1 of 2 bike/run sessions"
