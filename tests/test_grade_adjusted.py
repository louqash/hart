"""Grade-adjusted pace for outdoor runs: the grade profile, stored metrics, the v20 backfill and grading."""

from __future__ import annotations

import dataclasses
import datetime
import math
from pathlib import Path
from typing import Any

import pytest

from hart.analytics.grading_features import build_features
from hart.analytics.pace import _cost_of_transport, grade_adjusted_pace, grade_adjusted_run, run_grades
from hart.config import get_config
from hart.ingestion.grade_adjusted import GAP_COLUMNS, backfill_grade_adjusted
from hart.ingestion.sync_manager import SyncManager
from hart.models.activity import Activity, SportType, StreamPoint
from hart.server.state import DEFAULT_THRESHOLDS
from hart.storage.database import Database

EPOCH = 1_790_422_870  # activity_streams stores absolute epoch seconds
DAY = datetime.datetime(2026, 9, 20, 8, 0)
FLAT_PACE = 330.0  # s/km: 5:30/km on the flat
HR = 140
LAP_M = 500.0
WAVE_M = 1000.0


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "gap.duckdb").connect()
    yield database
    database.close()


def _ratio(grade: float) -> float:
    return _cost_of_transport(grade) / _cost_of_transport(0.0)


def _course(amplitude_m: float, km: float = 10.0) -> list[tuple[int, float, float, float]]:
    """(timestamp, distance m, altitude m, speed km/h) at 1 Hz for a run at one even effort.

    Rolling hills, one every kilometre: altitude = 100 - A·cos(2πd/1000), so each 500 m lap is all uphill
    or all downhill. The speed at every point is the flat speed divided by the Minetti cost ratio at the
    grade, so the grade-adjusted pace of the whole run is the flat pace.
    """
    v_flat = 1000 / FLAT_PACE
    k = 2 * math.pi / WAVE_M
    samples, d, t = [], 0.0, 0
    while d < km * 1000:
        v = v_flat / _ratio(amplitude_m * k * math.sin(k * d))
        samples.append((EPOCH + t, d, 100 - amplitude_m * math.cos(k * d), v * 3.6))
        d += v
        t += 1
    return samples


def _insert_run(
    db: Database, act_id: str, start: datetime.datetime, samples: list[tuple], sub: str | None = "street"
) -> None:
    """A run with its streams, 500 m laps (two warm-up laps, then work laps with a 5:30/km ±10 s target)."""
    secs, dist = len(samples), samples[-1][1]
    db.execute(
        "INSERT INTO activities (activity_id, source, sport_type, sub_type, name, start_time, elapsed_seconds, "
        "moving_seconds, distance_meters, avg_hr, avg_pace_sec_km) VALUES (?, 'test', 'run', ?, 'Run', ?, ?, ?, ?, "
        "?, ?)",
        [act_id, sub, start, secs, secs, dist, HR, secs / (dist / 1000)],
    )
    db.execute(
        "INSERT INTO activity_metrics (activity_id, sport_type, date, tss, efficiency_factor) "
        "VALUES (?, 'run', ?, 60, ?)",
        [act_id, start.date(), round(dist / secs / (HR / 60), 4)],
    )
    db.executemany(
        "INSERT INTO activity_streams (activity_id, timestamp_sec, heart_rate, distance, altitude, speed) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(act_id, t, HR, d, a, s) for t, d, a, s in samples],
    )
    laps: dict[int, list[tuple]] = {}
    for sample in samples:
        laps.setdefault(int(sample[1] // LAP_M), []).append(sample)
    ordered = [laps[i] for i in sorted(laps)]
    for i, lap in enumerate(ordered):
        end = ordered[i + 1][0][1] if i + 1 < len(ordered) else dist
        lap_dist = end - lap[0][1]
        db.execute(
            "INSERT INTO activity_laps (activity_id, lap_index, elapsed_seconds, moving_seconds, distance_meters, "
            "avg_hr, avg_pace_sec_km, intensity, wkt_step_index) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [act_id, i, len(lap), len(lap), lap_dist, HR, len(lap) / (lap_dist / 1000)]
            + (["warmup", 0] if i < 2 else ["active", 1]),
        )
    db.execute(
        "INSERT INTO activity_workout_steps (activity_id, step_index, name, intensity, target_type) "
        "VALUES (?, 0, 'Warm up', 'warmup', 'open')",
        [act_id],
    )
    db.execute(
        "INSERT INTO activity_workout_steps (activity_id, step_index, name, intensity, target_type, target_low, "
        "target_high, target_unit) VALUES (?, 1, 'Tempo', 'active', 'speed', 320, 340, 'sec_km')",
        [act_id],
    )


def _gap_row(db: Database, act_id: str) -> tuple[Any, ...]:
    return db.fetchone(f"SELECT {', '.join(GAP_COLUMNS)} FROM activity_metrics WHERE activity_id = ?", [act_id])


# ---------------------------------------------------------------------------
# The grade profile
# ---------------------------------------------------------------------------


def test_known_grade_gives_minetti_gap() -> None:
    distance = [i * 3.0 for i in range(2000)]  # 6 km at 3 m/s
    grades = run_grades(distance, [100 + 0.05 * d for d in distance])
    assert grades is not None
    assert grades.grade_pct == pytest.approx([5.0] * 2000)
    factor = grades.effort_factor()
    assert factor == pytest.approx(_ratio(0.05))
    # 6:00/km up a steady 5%: the same as the scalar Minetti GAP, 4:37/km.
    assert 360 / factor == pytest.approx(grade_adjusted_pace(360, 5.0))
    assert 360 / factor == pytest.approx(276.6, abs=0.1)
    gain, loss = grades.gain_loss()
    assert gain == pytest.approx(0.05 * distance[-1]) and loss == 0

    out = grade_adjusted_run(list(range(2000)), distance, [100 + 0.05 * d for d in distance], [10.8] * 2000)
    assert out["effort_factor"] == pytest.approx(factor)
    assert out["ngp_sec_km"] == pytest.approx(grade_adjusted_pace(3600 / 10.8, 5.0))  # steady: NGP = GAP


def test_flat_run_keeps_its_pace() -> None:
    distance = [i * 3.0 for i in range(2000)]
    grades = run_grades(distance, [42.0] * 2000)
    assert grades.effort_factor() == pytest.approx(1.0)
    assert grades.gain_loss() == (0.0, 0.0)


def test_grade_is_smoothed_and_clamped() -> None:
    distance = [i * 3.0 for i in range(1000)]
    cliff = run_grades(distance, [100 + 0.40 * d for d in distance])  # 40%: a glitch, not running
    assert max(cliff.grade_pct) == 25.0
    # A single 3 m altitude spike moves the grade by at most 3 m over the 100 m window.
    spiky = run_grades(distance, [100 + (3.0 if i == 500 else 0.0) for i in range(1000)])
    assert max(abs(g) for g in spiky.grade_pct) <= 3.0 + 1e-9


def test_a_recording_that_starts_again_counts_its_distance() -> None:
    """Joined sessions: the second recording's distance starts again at 0."""
    part = [i * 3.0 for i in range(700)]
    grades = run_grades(part + part, [100 + 0.02 * d for d in part] * 2)
    assert grades.total_m == pytest.approx(2 * part[-1])
    assert min(grades.increment_m) >= 0


def test_laps_get_their_own_grade() -> None:
    distance = [i * 2.0 for i in range(1001)]  # 2 km: flat, then 6% up
    altitude = [100 + (0.06 * (d - 1000) if d > 1000 else 0.0) for d in distance]
    flat, hill = run_grades(distance, altitude).laps([1000.0, 1000.0])
    assert flat["effort_factor"] == pytest.approx(1.0, abs=0.02)  # only its last 50 m feel the hill
    assert hill["effort_factor"] == pytest.approx(_ratio(0.06), rel=0.02)
    assert hill["gain_m"] == pytest.approx(60, abs=1) and hill["loss_m"] == 0


# ---------------------------------------------------------------------------
# Stored metrics: sync, backfill, migration
# ---------------------------------------------------------------------------


class _MetricsGarmin:
    def get_activity_hr_in_timezones(self, ext_id: str) -> list[dict[str, Any]]:
        return [{"zoneNumber": 2, "secsInZone": 3300.0}]


@pytest.mark.parametrize(("sub", "graded"), [(None, True), ("trail", True), ("treadmill", False)])
def test_sync_stores_gap_for_outdoor_runs(db: Database, sub: str | None, graded: bool) -> None:
    samples = _course(15)
    secs, dist = len(samples), samples[-1][1]
    points = [StreamPoint(timestamp_sec=t, heart_rate=HR, distance=d, altitude=a, speed=s) for t, d, a, s in samples]
    activity = Activity(
        activity_id="run",
        source="garmin",
        sport_type=SportType.run,
        sub_type=sub,
        start_time=DAY,
        elapsed_seconds=secs,
        avg_pace_sec_km=secs / (dist / 1000),
    )
    db.execute(
        "INSERT INTO activities (activity_id, source, sport_type, start_time, elapsed_seconds) "
        "VALUES ('run', 'garmin', 'run', ?, ?)",
        [DAY, secs],
    )
    summary = {"averageHR": HR, "averageMovingSpeed": dist / secs}

    SyncManager(db, get_config())._sync_activity_metrics(_MetricsGarmin(), "1", activity, summary, points)

    gap, ngp, gap_ef = _gap_row(db, "run")
    if not graded:
        assert (gap, ngp, gap_ef) == (None, None, None)
        return
    raw_ef = dist / secs / (HR / 60)
    assert activity.avg_pace_sec_km > FLAT_PACE * 1.04  # the hills made the actual pace slower
    assert gap == pytest.approx(FLAT_PACE, rel=0.01)  # the effort was 5:30/km on the flat
    assert gap_ef == pytest.approx(raw_ef * activity.avg_pace_sec_km / gap, rel=0.002)
    assert ngp < activity.avg_pace_sec_km


def test_backfill_fills_outdoor_runs_only(db: Database) -> None:
    _insert_run(db, "hilly", DAY, _course(15))
    _insert_run(db, "treadmill", DAY - datetime.timedelta(days=1), _course(15), sub="treadmill")
    _insert_run(db, "no_streams", DAY - datetime.timedelta(days=2), _course(0))
    db.execute("DELETE FROM activity_streams WHERE activity_id = 'no_streams'")

    assert backfill_grade_adjusted(db) == {"checked": 2, "computed": 1, "no_altitude": 1, "errors": 0}
    assert _gap_row(db, "hilly")[0] == pytest.approx(FLAT_PACE, rel=0.01)
    assert _gap_row(db, "treadmill") == (None, None, None)
    assert _gap_row(db, "no_streams") == (None, None, None)


def test_v20_migration_backfills_once_and_is_idempotent(tmp_path: Path) -> None:
    from hart.storage.schema import init_schema

    path = tmp_path / "v19.duckdb"
    db = Database(path).connect()
    _insert_run(db, "hilly", DAY, _course(15))
    # Make it look like a database from before v20: no GAP columns, no v20 row. (DuckDB won't drop a column
    # while an index depends on the table; connecting creates the index again before migrating, as on v19.)
    db.execute("DROP INDEX idx_activity_metrics_date")
    for column in GAP_COLUMNS:
        db.execute(f"ALTER TABLE activity_metrics DROP COLUMN {column}")
    db.execute("DELETE FROM schema_version WHERE version = 20")
    db.close()

    db = Database(path).connect()  # migrates and backfills
    first = _gap_row(db, "hilly")
    assert first[0] == pytest.approx(FLAT_PACE, rel=0.01) and None not in first
    db.execute("DELETE FROM schema_version WHERE version = 20")
    init_schema(db)  # running the step again changes nothing
    assert _gap_row(db, "hilly") == first
    assert db.fetchone("SELECT count(*) FROM schema_version WHERE version = 20")[0] == 1
    db.close()


# ---------------------------------------------------------------------------
# Grading features
# ---------------------------------------------------------------------------


def test_hilly_run_is_graded_on_grade_adjusted_pace(db: Database) -> None:
    for i in range(1, 5):
        _insert_run(db, f"flat{i}", DAY - datetime.timedelta(days=7 * i), _course(0))
    _insert_run(db, "hilly", DAY, _course(15))  # the same effort as the flat runs, over rolling hills
    backfill_grade_adjusted(db)

    f = build_features(db, "hilly", DEFAULT_THRESHOLDS)

    basics = f["basics"]
    assert basics["avg_pace_min_km"] > 5.7  # raw: slower than 5:30
    assert basics["gap_pace_min_km"] == pytest.approx(5.5, abs=0.05)
    assert basics["hill_effort_pct"] > 4 and basics["gap_efficiency_factor"] > f["durability"]["efficiency_factor"]

    comp = f["comparison"]
    pct = comp["this_vs_median_pct"]
    assert comp["count"] == 4 and comp["grade_adjusted_count"] == 4
    assert pct["avg_pace_sec_km"] > 4 and pct["ef"] < -4  # raw: looks slower and less efficient…
    assert abs(pct["gap_pace_sec_km"]) < 1.5 and abs(pct["gap_ef"]) < 1.5  # …but it isn't
    main = comp["main_block"]["this_vs_median_pct"]
    assert main["pace_sec_km"] > 4 and abs(main["gap_pace_sec_km"]) < 1.5 and abs(main["gap_ef"]) < 1.5

    intervals = f["intervals"]
    work = [lap for lap in intervals["laps"] if lap.get("kind") == "work"]
    assert len(work) == 18 and intervals["summary"]["pace_judged_on"].startswith("grade-adjusted")
    assert intervals["summary"]["on_target"] == 18  # uphill and downhill reps alike were on effort
    assert any(lap["pace_sec_km"] > 400 for lap in work)  # an uphill rep's raw pace is far off the target
    uphill = work[0]
    assert uphill["gain_m"] == pytest.approx(30, abs=1) and uphill["loss_m"] == 0
    assert abs(intervals["summary"]["last_vs_first_rep_pct"]) < 3  # no fading on the graded pace

    durability = f["durability"]
    assert durability["stream_decoupling"]["basis"] == "grade-adjusted speed/HR"
    assert durability["lap_variability"]["metric"] == "gap_pace_sec_km"
    assert durability["lap_variability"]["cv_pct"] < 3  # an even effort, though the pace swung


def test_indoor_run_is_untouched(db: Database) -> None:
    for i in range(1, 5):
        _insert_run(db, f"tm{i}", DAY - datetime.timedelta(days=7 * i), _course(0), sub="treadmill")
    _insert_run(db, "tm", DAY, _course(15), sub="treadmill")  # altitude moves, but it's a treadmill
    backfill_grade_adjusted(db)

    f = build_features(db, "tm", DEFAULT_THRESHOLDS)

    assert not any(k.startswith(("gap_", "ngp_", "hill_")) for k in f["basics"])
    comp = f["comparison"]
    assert "grade_adjusted_count" not in comp
    assert not any("gap" in k or "ngp" in k for k in (*comp["medians"], *comp["this_vs_median_pct"]))
    assert "gap_pace_sec_km" not in comp["main_block"]["this"]
    assert all("gap_pace_sec_km" not in lap and "gain_m" not in lap for lap in f["intervals"]["laps"])
    assert "pace_judged_on" not in f["intervals"]["summary"]
    assert f["durability"]["stream_decoupling"]["basis"] == "speed/HR"
    assert f["durability"]["lap_variability"]["metric"] == "avg_pace_sec_km"


# ---------------------------------------------------------------------------
# Session page and side-by-side compare
# ---------------------------------------------------------------------------

H = {"Tailscale-User-Login": "me@example.com"}


@pytest.fixture
def client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from hart.config import ServerSettings
    from hart.server.app import create_app
    from tests.test_chat import FakeClient

    db_path = tmp_path / "app.duckdb"
    seed = Database(db_path).connect()
    _insert_run(seed, "flat", DAY - datetime.timedelta(days=7), _course(0))
    _insert_run(seed, "hilly", DAY, _course(15))
    _insert_run(seed, "hilly_free", DAY - datetime.timedelta(days=1), _course(15))  # no structured workout
    seed.execute("DELETE FROM activity_workout_steps WHERE activity_id = 'hilly_free'")
    seed.execute("UPDATE activity_laps SET wkt_step_index = NULL, intensity = NULL WHERE activity_id = 'hilly_free'")
    _insert_run(seed, "tm_old", DAY - datetime.timedelta(days=7), _course(0), sub="treadmill")
    _insert_run(seed, "tm", DAY, _course(15), sub="treadmill")
    backfill_grade_adjusted(seed)
    seed.close()
    config = dataclasses.replace(
        get_config(), db_path=db_path, server=ServerSettings(env="production", seed_dir=tmp_path / "seed")
    )
    app = create_app(config, run_scheduler=False, claude_client_factory=FakeClient)
    with TestClient(app, base_url="https://hart.example.ts.net") as c:
        yield c


def test_session_page_shows_grade_adjusted_pace(client) -> None:
    page = client.get("/sessions/hilly", headers=H).text
    assert "Grade-adjusted pace" in page and "NGP 5:" in page  # stats: GAP and NGP
    assert "grade-adjusted" in page.split("Efficiency (EF)")[1][:600]  # EF with its grade-adjusted value
    intervals = page.split("Intervals")[1]
    assert ">GAP<" in intervals and "↑30 ↓0" in intervals and "↑0 ↓30" in intervals  # per-lap GAP and climb
    assert "Pace targets are judged on grade-adjusted pace" in intervals

    splits = client.get("/sessions/hilly_free", headers=H).text.split("Splits")[1]
    assert ">GAP<" in splits and "↑30 ↓0" in splits


def test_session_page_of_indoor_run_is_unchanged(client) -> None:
    page = client.get("/sessions/tm", headers=H).text
    assert "Grade-adjusted pace" not in page and ">GAP<" not in page and "↑" not in page
    assert "Pace targets are judged on grade-adjusted pace" not in page


def test_compare_judges_outdoor_runs_on_grade_adjusted_values(client) -> None:
    out = client.get("/api/sessions/hilly/compare?other=flat", headers=H).json()
    rows = {r["label"]: r for r in out["rows"]}
    assert out["grade_adjusted"] is True
    # Raw pace and EF look worse on the hillier route, but aren't marked down…
    assert rows["Pace"]["delta_pct"] > 4 and rows["Pace"]["verdict"] is None
    assert rows["Efficiency factor"]["delta_pct"] < -4 and rows["Efficiency factor"]["verdict"] is None
    # …the grade-adjusted ones show it was the same effort.
    assert abs(rows["Grade-adjusted pace"]["delta_pct"]) < 1.5 and abs(rows["Grade-adjusted EF"]["delta_pct"]) < 1.5
    assert "Normalized graded pace" in rows
    page = client.get("/sessions/hilly?compare=flat", headers=H).text
    assert "compared grade-adjusted" in page

    indoor = client.get("/api/sessions/tm/compare?other=tm_old", headers=H).json()
    labels = {r["label"]: r for r in indoor["rows"]}
    assert indoor["grade_adjusted"] is False
    assert not any("rade" in label for label in labels)  # no grade-adjusted rows on a treadmill
    assert labels["Pace"]["verdict"] == "bad"  # judged on pace, as before
