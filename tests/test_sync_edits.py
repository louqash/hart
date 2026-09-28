"""Edits made in Garmin Connect after an activity was synced, and the activity bar endpoint."""

from __future__ import annotations

import dataclasses
import datetime
from pathlib import Path

import pytest

from tests.test_grading import H
from hart.config import get_config
from hart.ingestion.sync_manager import SyncManager
from hart.storage.database import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "s.duckdb").connect()
    yield database
    database.close()


def _garmin_activity(db: Database, external_id: str, name: str, description: str | None = None) -> None:
    db.execute(
        "INSERT INTO activities (activity_id, source, external_id, sport_type, name, description, start_time, "
        "elapsed_seconds) VALUES (?, 'garmin', ?, 'run', ?, ?, ?, 1800)",
        [f"fit_{external_id}", external_id, name, description, datetime.datetime(2026, 9, 26, 8)],
    )


def test_renamed_activities_are_updated(db: Database) -> None:
    _garmin_activity(db, "111", "Morning Run")
    _garmin_activity(db, "222", "Indoor Cycling", "old notes")
    manager = SyncManager(db, get_config())
    changed = manager._refresh_edited([
        {"activityId": 111, "activityName": "Easy Z2 run"},
        {"activityId": 222, "activityName": "Indoor Cycling"},             # unchanged, no description sent
        {"activityId": 333, "activityName": "Not stored yet"},              # ignored
        {"activityId": 222, "activityName": "Indoor Cycling", "description": ""},  # empty never erases
    ])
    assert changed == 1
    names = dict(db.fetchall("SELECT external_id, name FROM activities"))
    assert names == {"111": "Easy Z2 run", "222": "Indoor Cycling"}
    assert db.fetchone("SELECT description FROM activities WHERE external_id = '222'")[0] == "old notes"
    manager._refresh_edited([{"activityId": 222, "activityName": "Trainer: sweet spot", "description": "legs heavy"}])
    assert db.fetchone("SELECT name, description FROM activities WHERE external_id = '222'") == (
        "Trainer: sweet spot", "legs heavy")


def test_active_jobs_endpoint(tmp_path: Path) -> None:
    import dataclasses

    from fastapi.testclient import TestClient

    from hart.config import ServerSettings
    from hart.server.app import create_app

    config = dataclasses.replace(get_config(), db_path=tmp_path / "a.duckdb",
                                 server=ServerSettings(env="production", seed_dir=tmp_path / "seed"))
    app = create_app(config, run_scheduler=False)
    with TestClient(app, base_url="https://hart.example.ts.net") as client:
        assert client.get("/api/jobs/active", headers=H).json()["active"] == []
        job = app.state.runner.enqueue("backup", {}, trigger="manual", dedupe_key="backup")
        active = client.get("/api/jobs/active", headers=H).json()["active"]
        assert not active or active[0]["id"] == job["job_id"]


def test_edited_strength_sets_are_refreshed(db: Database, monkeypatch) -> None:
    from hart.ingestion import sync_manager as sm
    from hart.models.activity import StrengthSet
    from hart.storage.writers import replace_strength_sets

    db.execute(
        "INSERT INTO activities (activity_id, source, external_id, sport_type, name, start_time, elapsed_seconds) "
        "VALUES ('gym', 'garmin', '555', 'strength', 'Session B', ?, 3000)",
        [datetime.datetime.now() - datetime.timedelta(hours=5)],
    )
    first = [StrengthSet(set_index=0, set_type="active", repetitions=8, weight_kg=60.0, exercise_name="DEADLIFT")]
    replace_strength_sets(db, "gym", first)
    garmin_sets = {"555": first}
    config = dataclasses.replace(get_config(), garmin=dataclasses.replace(get_config().garmin, email="x@example.com"))
    manager = SyncManager(db, config)
    monkeypatch.setattr(manager, "get_garmin_client", lambda: object())
    monkeypatch.setattr(sm, "_fetch_strength_sets", lambda garmin, act_id: garmin_sets[act_id])

    assert manager.refresh_recent_strength_sets(days=3) == []  # unchanged → nothing written
    garmin_sets["555"] = [StrengthSet(set_index=0, set_type="active", repetitions=5, weight_kg=62.5,
                                      exercise_name="DEADLIFT"),
                          StrengthSet(set_index=1, set_type="active", repetitions=10, weight_kg=20.0,
                                      exercise_name="GOBLET_SQUAT")]
    assert manager.refresh_recent_strength_sets(days=3) == ["gym"]
    assert db.fetchall("SELECT repetitions, weight_kg FROM strength_sets WHERE activity_id = 'gym' ORDER BY set_index") == [
        (5, 62.5), (10, 20.0)]
    assert manager.refresh_recent_strength_sets(days=3, skip={"gym"}) == []


def test_sync_regrades_sessions_with_edited_sets(tmp_path: Path) -> None:
    from hart.server.jobs.handlers import make_handlers
    from tests.test_server import FakeSyncManager

    class Runner:
        calls: list = []

        def enqueue(self, job_type, payload=None, **kw):
            Runner.calls.append((job_type, payload))
            return {"job_id": 1, "status": "queued"}

    db = Database(tmp_path / "h.duckdb").connect()
    db.execute("INSERT INTO session_grades (activity_id, version, status) VALUES ('graded_gym', 1, 'graded')")
    FakeSyncManager.health_error = None
    FakeSyncManager.new_activity_day = None
    FakeSyncManager.edited_strength = ["graded_gym", "ungraded_gym"]
    FakeSyncManager.edited_effort = ["graded_run", "run_with_feedback", "graded_gym"]
    for a in ("graded_run", "run_with_feedback"):
        db.execute("INSERT INTO session_grades (activity_id, version, status) VALUES (?, 1, 'graded')", [a])
    db.execute("INSERT INTO session_feedback (activity_id, rpe) VALUES ('run_with_feedback', 6)")
    config = dataclasses.replace(get_config(), garmin=dataclasses.replace(get_config().garmin, email="x@example.com"))
    import hart.server.jobs.handlers as handlers_mod
    original = handlers_mod.run_sync_pipeline
    handlers_mod.run_sync_pipeline = lambda db, config, **kw: original(db, config, sync_manager_factory=FakeSyncManager, **kw)
    try:
        make_handlers(config, {"runner": Runner()})["sync"](db, {})
    finally:
        handlers_mod.run_sync_pipeline = original
        FakeSyncManager.edited_strength = []
        FakeSyncManager.edited_effort = []
    grades = [(p["activity_id"], p["trigger"]) for t, p in Runner.calls if t == "grade"]
    # Only graded sessions; Garmin RPE is ignored where the app has your own feedback; no double regrade.
    assert grades == [("graded_gym", "sets_edited"), ("graded_run", "effort_edited")]
    db.close()


def test_edited_rpe_and_feel_are_refreshed(db: Database, monkeypatch) -> None:
    db.execute(
        "INSERT INTO activities (activity_id, source, external_id, sport_type, name, start_time, elapsed_seconds, rpe, feel) "
        "VALUES ('run', 'garmin', '777', 'run', 'Easy run', ?, 1800, NULL, NULL)",
        [datetime.datetime.now() - datetime.timedelta(hours=3)],
    )
    answers: dict = {"777": {"summaryDTO": {"directWorkoutRpe": None, "directWorkoutFeel": None}}}

    class Garmin:
        def get_activity(self, act_id):
            return answers[act_id]

    config = dataclasses.replace(get_config(), garmin=dataclasses.replace(get_config().garmin, email="x@example.com"))
    manager = SyncManager(db, config)
    monkeypatch.setattr(manager, "get_garmin_client", lambda: Garmin())
    assert manager.refresh_recent_effort(days=3) == []
    answers["777"] = {"summaryDTO": {"directWorkoutRpe": 40, "directWorkoutFeel": 75}}
    assert manager.refresh_recent_effort(days=3) == ["run"]
    assert db.fetchone("SELECT rpe, feel FROM activities WHERE activity_id = 'run'") == (40, 75)
    answers["777"] = {}  # Garmin didn't answer properly: keep what we have
    assert manager.refresh_recent_effort(days=3) == []
    assert db.fetchone("SELECT rpe, feel FROM activities WHERE activity_id = 'run'") == (40, 75)


def test_auth_detects_garmin_rate_limit_page() -> None:
    from hart.interfaces.cli.app import GarminBlocked, _check_not_blocked

    class Page:
        def __init__(self, text: str) -> None:
            self.text = text

        def inner_text(self, selector: str, timeout: int) -> str:
            return self.text

    _check_not_blocked(Page("Sign in to Garmin Connect"))  # normal page: no error
    with pytest.raises(GarminBlocked, match="1015"):
        _check_not_blocked(Page("Error 1015 Ray ID: x • You are being rate limited"))
