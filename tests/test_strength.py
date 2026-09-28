"""Strength progression page."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from hart.analytics.strength_progress import e1rm, exercise_key, progression, weekly_sessions
from hart.storage.database import Database

TODAY = datetime.date(2026, 9, 28)


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "st.duckdb").connect()
    yield database
    database.close()


def _gym(
    db: Database, aid: str, day: datetime.date, sets: list[tuple[str | None, str | None, int, float | None]]
) -> None:
    db.execute(
        "INSERT INTO activities (activity_id, source, sport_type, name, start_time, elapsed_seconds) "
        "VALUES (?, 'test', 'strength', 'Gym', ?, 3000)",
        [aid, datetime.datetime.combine(day, datetime.time(18))],
    )
    for i, (name, category, reps, kg) in enumerate(sets):
        db.execute(
            "INSERT INTO strength_sets (activity_id, set_index, set_type, repetitions, weight_kg, exercise_name, "
            "exercise_category) VALUES (?, ?, 'active', ?, ?, ?, ?)",
            [aid, i, reps, kg, name, category],
        )


def test_helpers() -> None:
    assert e1rm(60, 5) == 70.0 and e1rm(60, 15) is None and e1rm(0, 5) is None
    assert exercise_key(None, "DEADLIFT") == "DEADLIFT" == exercise_key("BARBELL_DEADLIFT", "DEADLIFT")
    assert exercise_key("BARBELL_BENCH_PRESS", "BENCH_PRESS") == "BENCH_PRESS"
    assert exercise_key("ROMANIAN_DEADLIFT", "DEADLIFT") == "ROMANIAN_DEADLIFT"  # a different lift
    assert exercise_key("DUMBBELL_BENCH_PRESS", "BENCH_PRESS") == "DUMBBELL_BENCH_PRESS"  # different load
    assert exercise_key("WALK", "RUN") is None and exercise_key(None, None) is None
    assert exercise_key(None, "SQUAT", {"SQUAT": "WEIGHTED_BACK_SQUATS"}) == "WEIGHTED_BACK_SQUATS"
    assert exercise_key("A", None, {"A": "B", "B": "A"}) in ("A", "B")  # a cycle doesn't hang


def test_progression_and_weeks(db: Database) -> None:
    _gym(
        db,
        "a",
        TODAY - datetime.timedelta(days=30),
        [
            ("BARBELL_DEADLIFT", "DEADLIFT", 5, 50),
            ("BARBELL_DEADLIFT", "DEADLIFT", 5, 60),
            ("PULL_UP", "PULL_UP", 6, 0),
            ("WALK", "RUN", 1, None),
        ],
    )
    _gym(
        db,
        "b",
        TODAY - datetime.timedelta(days=3),
        [(None, "DEADLIFT", 5, 65), ("PULL_UP", "PULL_UP", 8, 0), (None, "SQUAT", 10, 20)],
    )
    p = progression(db, TODAY)
    by_key = {e["key"]: e for e in p["exercises"]}
    # "BARBELL_DEADLIFT" (session a) and category-only "DEADLIFT" (session b) are one lift; walking isn't a lift.
    assert set(by_key) == {"DEADLIFT", "PULL_UP", "SQUAT"}
    dl = by_key["DEADLIFT"]
    assert dl["latest"]["top_kg"] == 65 and dl["history"][0]["top_kg"] == 60 and dl["history"][0]["volume"] == 550
    assert dl["block_change_pct"] == round((75.8 / 70.0 - 1) * 100, 1)
    pull = by_key["PULL_UP"]
    assert pull["bodyweight"] and pull["metric"] == "max_reps" and pull["block_change_pct"] == 33.3
    assert by_key["SQUAT"]["label"] == "Squat" and dl["label"] == "Deadlift"
    weeks = weekly_sessions(db, TODAY)
    assert len(weeks) == 8 and sum(w["sessions"] for w in weeks) == 2


def test_same_lift_merge_via_api(tmp_path: Path) -> None:
    import dataclasses

    from fastapi.testclient import TestClient

    from hart.config import ServerSettings, get_config
    from hart.server.app import create_app
    from tests.test_grading import H, W

    db_path = tmp_path / "a.duckdb"
    seed = Database(db_path).connect()
    _gym(seed, "a", TODAY - datetime.timedelta(days=10), [(None, "SQUAT", 5, 60)])
    _gym(seed, "b", TODAY - datetime.timedelta(days=3), [("WEIGHTED_BACK_SQUATS", "SQUAT", 5, 70)])
    seed.close()
    config = dataclasses.replace(
        get_config(), db_path=db_path, server=ServerSettings(env="production", seed_dir=tmp_path / "seed")
    )
    with TestClient(create_app(config, run_scheduler=False), base_url="https://hart.example.ts.net") as client:
        keys = {e["key"] for e in client.get("/api/strength", headers=H).json()["exercises"]}
        assert keys == {"SQUAT", "WEIGHTED_BACK_SQUATS"}
        client.post("/api/strength/aliases", headers=W, json={"name": "SQUAT", "same_as": "WEIGHTED_BACK_SQUATS"})
        merged = client.get("/api/strength", headers=H).json()["exercises"]
        assert [(e["key"], e["sessions"]) for e in merged] == [("WEIGHTED_BACK_SQUATS", 2)]
        cycle = client.post(
            "/api/strength/aliases", headers=W, json={"name": "WEIGHTED_BACK_SQUATS", "same_as": "SQUAT"}
        )
        assert cycle.status_code == 400
        assert "Squat → Weighted Back Squats" in client.get("/strength", headers=H).text
        client.post("/api/strength/aliases", headers=W, json={"name": "SQUAT", "same_as": None})
        assert len(client.get("/api/strength", headers=H).json()["exercises"]) == 2


def test_strength_history_tool_merges_lifts(tmp_path: Path) -> None:
    import json

    from hart import mcp_server

    db = Database(tmp_path / "m.duckdb").connect()
    _gym(db, "a", datetime.date.today() - datetime.timedelta(days=10), [("BARBELL_DEADLIFT", "DEADLIFT", 5, 60)])
    _gym(
        db,
        "b",
        datetime.date.today() - datetime.timedelta(days=3),
        [(None, "DEADLIFT", 5, 65), ("ROMANIAN_DEADLIFT", "DEADLIFT", 8, 40)],
    )
    mcp_server.configure_server(db, lambda *a, **k: {}, lambda _id: None)
    try:
        out = json.loads(mcp_server.get_strength_history(days_back=30, exercise="deadlift"))
    finally:
        mcp_server.configure_server(None, None, None)
    lifts = out["exercise_progression"]
    assert set(lifts) == {"Deadlift", "Romanian Deadlift"}
    assert len(lifts["Deadlift"]["sessions"]) == 2
    assert lifts["Deadlift"]["recorded_as"] == ["Barbell Deadlift", "Deadlift"]
    db.close()


def test_demo_database(tmp_path: Path) -> None:
    from hart.demo import build
    from hart.server import settings, state
    from hart.server.jobs.runner import DEMO_BLOCKED

    out = build(tmp_path / "demo.duckdb", today=TODAY)
    assert out["activities"] > 150
    db = Database(tmp_path / "demo.duckdb").connect()
    assert state.is_demo(db) and settings.get(db, "athlete_name") == "Alex"
    assert db.fetchone("SELECT count(*) FROM daily_training_load WHERE sport_type = 'combined'")[0] > 150
    assert db.fetchone("SELECT count(*) FROM races")[0] == 2
    assert db.fetchone("SELECT count(*) FROM session_grades WHERE status = 'graded'")[0] >= 5
    assert "sync" in DEMO_BLOCKED
    db.close()


def test_demo_database_never_reads_seed_files(tmp_path: Path) -> None:
    """Starting the server on a demo database must not import the (real) seed folder."""
    import dataclasses
    import json

    from fastapi.testclient import TestClient

    from hart.config import ServerSettings, get_config
    from hart.demo import build
    from hart.server.app import create_app

    build(tmp_path / "demo.duckdb", today=TODAY)
    real = tmp_path / "real-seed"
    real.mkdir()
    (real / "lab_results.json").write_text(
        json.dumps([{"date": "2020-01-01", "markers": [{"name": "Hemoglobin", "value": "99.9", "unit": "g/dl"}]}])
    )
    (tmp_path / "blood_results.json").write_text("[]")
    config = dataclasses.replace(
        get_config(), db_path=tmp_path / "demo.duckdb", server=ServerSettings(env="production", seed_dir=real)
    )
    with TestClient(create_app(config, run_scheduler=False), base_url="https://hart.example.ts.net"):
        pass
    db = Database(tmp_path / "demo.duckdb").connect()
    assert db.fetchone("SELECT count(*) FROM lab_results WHERE value_text = '99.9'")[0] == 0
    db.close()
