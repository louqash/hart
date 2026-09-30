"""Interval breakdown: FIT workout steps and laps against their targets."""

from __future__ import annotations

from hart.analytics.intervals import breakdown, verdict
from hart.ingestion.fit_parser import workout_step_from_fit


def test_workout_steps_from_fit() -> None:
    power = workout_step_from_fit(
        {
            "message_index": 1,
            "wkt_step_name": "4x8'",
            "intensity": "active",
            "duration_type": "time",
            "duration_time": 480.0,
            "target_type": "power",
            "custom_target_power_low": 1240,
            "custom_target_power_high": 1250,
        },
        0,
    )
    assert (power.target_low, power.target_high, power.target_unit, power.duration_value) == (240, 250, "W", 480.0)
    ftp = workout_step_from_fit(
        {"target_type": "power", "custom_target_power_low": 88, "custom_target_power_high": 94}, 2
    )
    assert (ftp.step_index, ftp.target_low, ftp.target_unit) == (2, 88, "%FTP")
    hr = workout_step_from_fit(
        {"target_type": "heart_rate", "custom_target_heart_rate_low": 240, "custom_target_heart_rate_high": 250}, 0
    )
    assert (hr.target_low, hr.target_high, hr.target_unit) == (140, 150, "bpm")  # FIT spec: bpm + 100
    plain = workout_step_from_fit(
        {"target_type": "heart_rate", "custom_target_heart_rate_low": 120, "custom_target_heart_rate_high": 140}, 0
    )
    assert (plain.target_low, plain.target_high, plain.target_unit) == (120, 140, "bpm")  # watches write plain bpm
    high = workout_step_from_fit(
        {"target_type": "heart_rate", "custom_target_heart_rate_low": 170, "custom_target_heart_rate_high": 185}, 0
    )
    assert (high.target_low, high.target_high) == (170, 185)  # not 70-85
    pace = workout_step_from_fit(
        {"target_type": "speed", "custom_target_speed_low": 3.333, "custom_target_speed_high": 3.571}, 0
    )
    assert (pace.target_low, pace.target_high, pace.target_unit) == (280.0, 300.0, "sec_km")  # 4:40–5:00 /km
    zone = workout_step_from_fit({"target_type": "heart_rate", "target_hr_zone": 2}, 0)
    assert (zone.target_low, zone.target_unit) == (2, "zone")
    repeat = workout_step_from_fit(
        {"duration_type": "repeat_until_steps_cmplt", "duration_step": 1, "repeat_steps": 4}, 3
    )
    assert (repeat.repeat_from, repeat.repeat_count) == (1, 4)


def test_verdicts() -> None:
    assert verdict(245, 240, 250, "avg_power") == ("on", 0.0)
    assert verdict(236, 240, 250, "avg_power") == ("on", 0.0)  # within the 2 % tolerance
    assert verdict(225, 240, 250, "avg_power") == ("under", 6.2)
    assert verdict(265, 240, 250, "avg_power") == ("over", 6.0)
    assert verdict(310, 280, 300, "avg_pace_sec_km") == ("under", 3.3)  # slower than 5:00/km
    assert verdict(250, 280, 300, "avg_pace_sec_km")[0] == "over"


def _lap(i: int, secs: int, power: float, hr: float, intensity: str, step: int) -> dict:
    return {
        "lap_index": i,
        "elapsed_seconds": secs,
        "moving_seconds": secs,
        "avg_power": power,
        "avg_hr": hr,
        "intensity": intensity,
        "wkt_step_index": step,
    }


def test_breakdown_of_a_structured_workout() -> None:
    steps = [
        {"step_index": 0, "name": "Warm up", "intensity": "warmup", "target_type": "open"},
        {
            "step_index": 1,
            "name": "8' threshold",
            "intensity": "active",
            "target_type": "power",
            "target_low": 240,
            "target_high": 250,
            "target_unit": "W",
        },
        {"step_index": 2, "name": "Easy", "intensity": "rest", "target_type": "open"},
        {"step_index": 3, "name": "Cool down", "intensity": "cooldown", "target_type": "open"},
    ]
    laps = [_lap(0, 600, 150, 120, "warmup", 0)]
    for i, (p, hr) in enumerate([(246, 158), (244, 161), (238, 165), (222, 168)]):
        laps += [_lap(1 + 2 * i, 480, p, hr, "active", 1), _lap(2 + 2 * i, 180, 120, 130, "rest", 2)]
    laps.append(_lap(9, 600, 140, 125, "cooldown", 3))
    out = breakdown("bike", laps, steps)
    work = [r for r in out["laps"] if r.get("kind") == "work"]
    assert [r["rep"] for r in work] == [1, 2, 3, 4]
    assert [r["verdict"] for r in work] == ["on", "on", "on", "under"]
    assert work[0]["target"] == {"low": 240, "high": 250, "unit": "W"} and work[0]["step"] == "8' threshold"
    s = out["summary"]
    assert s["source"] == "workout" and (s["work_reps"], s["on_target"], s["under"]) == (4, 3, 1)
    assert s["last_vs_first_rep_pct"] == -9.8 and s["hr_drift_bpm"] == 10 and s["avg_recovery_seconds"] == 180


def test_breakdown_of_manual_laps() -> None:
    laps = [{"lap_index": i, "elapsed_seconds": 300, "avg_pace_sec_km": 300 + i, "avg_hr": 150} for i in range(3)]
    out = breakdown("run", laps, [])
    assert out["summary"]["source"] == "laps" and out["summary"]["work_reps"] == 0
    assert "verdict" not in out["laps"][0] and out["laps"][2]["pace_sec_km"] == 302
    assert breakdown("run", laps[:1], []) is None


def _store_workout(db) -> None:
    import datetime

    from hart.models import Lap, WorkoutStep
    from hart.storage.writers import replace_workout_steps, upsert_laps

    db.execute(
        "INSERT INTO activities (activity_id, source, external_id, sport_type, sub_type, name, start_time, "
        "elapsed_seconds, moving_seconds, avg_hr, avg_power) VALUES ('ride', 'garmin', '9', 'bike', "
        "'indoor_cycling', 'Threshold', ?, 3000, 3000, 140, 190)",
        [datetime.datetime.now() - datetime.timedelta(hours=3)],
    )
    steps = [
        WorkoutStep(step_index=0, name="Warm up", intensity="warmup", target_type="open"),
        WorkoutStep(
            step_index=1,
            name="8' threshold",
            intensity="active",
            target_type="power",
            target_low=240,
            target_high=250,
            target_unit="W",
        ),
        WorkoutStep(step_index=2, name="Easy", intensity="rest", target_type="open"),
    ]
    laps = [Lap(lap_index=0, elapsed_seconds=600, avg_power=150, avg_hr=120, intensity="warmup", wkt_step_index=0)]
    for i, p in enumerate([246, 243, 221]):
        laps.append(
            Lap(
                lap_index=1 + 2 * i,
                elapsed_seconds=480,
                avg_power=p,
                avg_hr=158 + i * 4,
                intensity="active",
                wkt_step_index=1,
            )
        )
        laps.append(
            Lap(lap_index=2 + 2 * i, elapsed_seconds=180, avg_power=110, avg_hr=128, intensity="rest", wkt_step_index=2)
        )
    upsert_laps(db, "ride", laps)
    replace_workout_steps(db, "ride", steps)


def test_grader_and_session_page_see_the_intervals(tmp_path) -> None:
    import dataclasses

    from fastapi.testclient import TestClient

    from hart.analytics.grading_features import build_features
    from hart.config import ServerSettings, get_config
    from hart.server.app import create_app
    from hart.server.state import DEFAULT_THRESHOLDS
    from hart.storage.database import Database

    db = Database(tmp_path / "i.duckdb").connect()
    _store_workout(db)
    f = build_features(db, "ride", DEFAULT_THRESHOLDS)
    assert f["intervals"]["summary"]["work_reps"] == 3 and f["intervals"]["summary"]["under"] == 1
    db.close()
    config = dataclasses.replace(
        get_config(), db_path=tmp_path / "i.duckdb", server=ServerSettings(env="production", seed_dir=tmp_path / "s")
    )
    with TestClient(create_app(config, run_scheduler=False), base_url="https://hart.example.ts.net") as c:
        page = c.get("/sessions/ride", headers={"Tailscale-User-Login": "a@example.com"}).text
    assert "Intervals" in page and "240–250 W" in page and "under 7.9%" in page and "Rep 3" in page
    assert "Planned" in page and "On the watch" in page and "8&#39; threshold" in page and "8′" in page


def test_intervals_backfill(tmp_path, monkeypatch) -> None:
    from hart.config import get_config
    from hart.ingestion import fit_parser, sync_manager
    from hart.ingestion.fit_parser import FitParseResult
    from hart.models import Activity, Lap, SportType, WorkoutStep
    from hart.storage.database import Database

    db = Database(tmp_path / "b.duckdb").connect()
    _store_workout(db)
    db.execute("DELETE FROM activity_workout_steps")
    db.execute("UPDATE activity_laps SET intensity = NULL, wkt_step_index = NULL")

    class Garmin:
        def download_activity(self, activity_id, dl_fmt=None):
            return b"fit"

    parsed = FitParseResult(
        activity=Activity(
            activity_id="x",
            source="garmin",
            sport_type=SportType.bike,
            start_time="2026-09-01T07:00",
            elapsed_seconds=60,
        ),
        stream_points=[],
        laps=[Lap(lap_index=1, elapsed_seconds=480, avg_power=246, intensity="active", wkt_step_index=1)],
        hrv_rr_intervals=[],
        device_info={},
        workout_steps=[
            WorkoutStep(
                step_index=1, intensity="active", target_type="power", target_low=240, target_high=250, target_unit="W"
            )
        ],
    )
    monkeypatch.setattr(sync_manager, "_extract_fit", lambda data: data)
    monkeypatch.setattr(fit_parser.FitParser, "parse_file", lambda self, path: parsed)
    manager = sync_manager.SyncManager(db, get_config())
    monkeypatch.setattr(manager, "get_garmin_client", lambda: Garmin())
    assert manager.backfill_intervals(days=30) == {"sessions": 1, "updated": 1, "with_workout": 1, "errors": 0}
    assert db.fetchone("SELECT intensity, wkt_step_index FROM activity_laps WHERE lap_index = 1") == ("active", 1)
    assert db.fetchone("SELECT count(*) FROM activity_workout_steps")[0] == 1
    db.close()


def test_plain_watts_and_the_lap_after_the_workout() -> None:
    steps = [
        {"step_index": 0, "intensity": "warmup", "target_type": "open"},
        {
            "step_index": 1,
            "intensity": "active",
            "target_type": "power",
            "target_low": 230,
            "target_high": 240,
            "target_unit": "%FTP",
        },  # the watch wrote plain watts
    ]
    laps = [
        {"lap_index": 0, "elapsed_seconds": 600, "avg_power": 150, "intensity": "warmup", "wkt_step_index": 0},
        {"lap_index": 1, "elapsed_seconds": 720, "avg_power": 236, "intensity": "active", "wkt_step_index": 1},
        {"lap_index": 2, "elapsed_seconds": 4, "avg_power": 78},  # after the workout ended
    ]
    out = breakdown("bike", laps, steps)
    assert len(out["laps"]) == 2
    rep = out["laps"][1]
    assert rep["target"]["unit"] == "W" and rep["verdict"] == "on"


def test_absurd_misses_are_not_judged() -> None:
    steps = [
        {
            "step_index": 1,
            "intensity": "active",
            "target_type": "heart_rate",
            "target_low": 20,
            "target_high": 40,
            "target_unit": "bpm",
        }
    ]
    laps = [
        {"lap_index": i, "elapsed_seconds": 600, "avg_hr": 122, "intensity": "active", "wkt_step_index": 1}
        for i in range(2)
    ]
    rep = breakdown("bike", laps, steps)["laps"][0]
    assert "verdict" not in rep and rep["target_doubtful"] is True
