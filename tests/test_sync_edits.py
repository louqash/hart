"""Edits made in Garmin Connect after an activity was synced, and the activity bar endpoint."""

from __future__ import annotations

import dataclasses
import datetime
from pathlib import Path

import pytest

from hart.config import get_config
from hart.ingestion.sync_manager import SyncManager
from hart.storage.database import Database
from tests.test_grading import H


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
    changed = manager._refresh_edited(
        [
            {"activityId": 111, "activityName": "Easy Z2 run"},
            {"activityId": 222, "activityName": "Indoor Cycling"},  # unchanged, no description sent
            {"activityId": 333, "activityName": "Not stored yet"},  # ignored
            {"activityId": 222, "activityName": "Indoor Cycling", "description": ""},  # empty never erases
        ]
    )
    assert changed == 1
    names = dict(db.fetchall("SELECT external_id, name FROM activities"))
    assert names == {"111": "Easy Z2 run", "222": "Indoor Cycling"}
    assert db.fetchone("SELECT description FROM activities WHERE external_id = '222'")[0] == "old notes"
    described: list[str] = []
    manager._refresh_edited(
        [{"activityId": 222, "activityName": "Trainer: sweet spot", "description": "legs heavy"}], described
    )
    assert described == ["fit_222"]  # a new description means a re-grade
    renamed_only: list[str] = []
    manager._refresh_edited([{"activityId": 111, "activityName": "Z2 run"}], renamed_only)
    assert renamed_only == []  # a rename alone doesn't
    assert db.fetchone("SELECT name, description FROM activities WHERE external_id = '222'") == (
        "Trainer: sweet spot",
        "legs heavy",
    )


def test_active_jobs_endpoint(tmp_path: Path) -> None:
    import dataclasses

    from fastapi.testclient import TestClient

    from hart.config import ServerSettings
    from hart.server.app import create_app

    config = dataclasses.replace(
        get_config(), db_path=tmp_path / "a.duckdb", server=ServerSettings(env="production", seed_dir=tmp_path / "seed")
    )
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
    garmin_sets["555"] = [
        StrengthSet(set_index=0, set_type="active", repetitions=5, weight_kg=62.5, exercise_name="DEADLIFT"),
        StrengthSet(set_index=1, set_type="active", repetitions=10, weight_kg=20.0, exercise_name="GOBLET_SQUAT"),
    ]
    assert manager.refresh_recent_strength_sets(days=3) == ["gym"]
    assert db.fetchall(
        "SELECT repetitions, weight_kg FROM strength_sets WHERE activity_id = 'gym' ORDER BY set_index"
    ) == [(5, 62.5), (10, 20.0)]
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
    handlers_mod.run_sync_pipeline = lambda db, config, **kw: original(
        db, config, sync_manager_factory=FakeSyncManager, **kw
    )
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


# ---------------------------------------------------------------------------
# Activities deleted in Garmin Connect
# ---------------------------------------------------------------------------


class FakeGarmin:
    """Garmin Connect as the activity sync sees it: the list for the window, then a lookup per activity."""

    def __init__(self, listed: list[str] | Exception, gone: set[str] = frozenset(), failing: set[str] = frozenset()):
        self.listed = listed
        self.gone = gone  # deleted: the lookup says "not found"
        self.failing = failing  # the lookup fails some other way
        self.lookups: list[str] = []

    def get_activities_by_date(self, start: str, end: str) -> list[dict]:
        if isinstance(self.listed, Exception):
            raise self.listed
        return [{"activityId": int(i)} for i in self.listed]

    def get_activity(self, act_id: str) -> dict:
        from garminconnect import GarminConnectConnectionError

        self.lookups.append(act_id)
        if act_id in self.gone:
            raise GarminConnectConnectionError("HTTP error: API Error 404 - Not Found")
        if act_id in self.failing:
            raise GarminConnectConnectionError("HTTP error: API Error 429 - Too Many Requests")
        return {"activityId": int(act_id), "summaryDTO": {}}


def _stored(db: Database, activity_id: str, external_id: str, days_ago: int = 1, source: str = "garmin") -> None:
    db.execute(
        "INSERT INTO activities (activity_id, source, external_id, sport_type, name, start_time, elapsed_seconds) "
        "VALUES (?, ?, ?, 'run', 'Easy run', ?, 1800)",
        [activity_id, source, external_id, datetime.datetime.now() - datetime.timedelta(days=days_ago)],
    )


def _with_dependants(db: Database, a: str) -> None:
    """One row in every table that belongs to activity *a*, and a planned session matched to it."""
    db.execute("INSERT INTO activity_streams (activity_id, timestamp_sec, heart_rate) VALUES (?, 0, 120)", [a])
    db.execute("INSERT INTO activity_laps (activity_id, lap_index, elapsed_seconds) VALUES (?, 0, 600)", [a])
    db.execute("INSERT INTO activity_workout_steps (activity_id, step_index) VALUES (?, 0)", [a])
    db.execute("INSERT INTO strength_sets (activity_id, set_index, set_type) VALUES (?, 0, 'active')", [a])
    db.execute("INSERT INTO hrv_samples (activity_id, sample_index, rr_interval_ms) VALUES (?, 0, 800)", [a])
    db.execute(
        "INSERT INTO activity_metrics (activity_id, sport_type, date, tss) VALUES (?, 'run', current_date, 40)", [a]
    )
    db.execute("INSERT INTO session_feedback (activity_id, rpe) VALUES (?, 5)", [a])
    db.execute("INSERT INTO session_grades (activity_id, version, status) VALUES (?, 1, 'graded')", [a])
    db.execute("INSERT INTO grade_waits (activity_id, grade_at, missing) VALUES (?, now(), 'note')", [a])
    db.execute(
        "INSERT INTO anomaly_log (anomaly_type, severity, metric_name, activity_id) VALUES ('hr_drift', 'low', 'hr', ?)",
        [a],
    )
    db.execute(
        "INSERT INTO planned_sessions (date, sport_type, title, source, activity_id, match_method) "
        "VALUES (current_date, 'run', 'Easy run', 'coach', ?, 'auto')",
        [a],
    )


def _dependants(db: Database, a: str) -> dict[str, int]:
    from hart.storage.writers import _ACTIVITY_CHILD_TABLES

    counts = {t: db.fetchone(f"SELECT count(*) FROM {t} WHERE activity_id = ?", [a])[0] for t in _ACTIVITY_CHILD_TABLES}
    counts["planned_sessions"] = db.fetchone("SELECT count(*) FROM planned_sessions WHERE activity_id = ?", [a])[0]
    return counts


def _syncing(db: Database, monkeypatch, garmin: FakeGarmin) -> SyncManager:
    config = dataclasses.replace(get_config(), garmin=dataclasses.replace(get_config().garmin, email="x@example.com"))
    manager = SyncManager(db, config)
    monkeypatch.setattr(manager, "get_garmin_client", lambda: garmin)
    return manager


def _stored_ids(db: Database) -> set[str]:
    return {r[0] for r in db.fetchall("SELECT activity_id FROM activities")}


def test_delete_activity_covers_every_table_keyed_by_activity(db: Database) -> None:
    from hart.storage.writers import _ACTIVITY_CHILD_TABLES

    tables = {
        r[0]
        for r in db.fetchall(
            "SELECT DISTINCT table_name FROM information_schema.columns WHERE column_name = 'activity_id'"
        )
        if not r[0].startswith("mv_")  # materialised views are rebuilt from the tables
    }
    # A new table with an activity_id column belongs in delete_activity too.
    assert tables == {*_ACTIVITY_CHILD_TABLES, "activities", "planned_sessions"}


def test_activities_deleted_in_garmin_are_removed(db: Database, monkeypatch) -> None:
    _stored(db, "listed", "101")
    _stored(db, "deleted", "102")
    _stored(db, "still_there", "103")  # missing from the list, but Garmin still has it
    _stored(db, "old", "104", days_ago=30)  # outside the window
    _stored(db, "imported", "105", source="fit")  # not a Garmin activity
    for a in ("listed", "deleted"):
        _with_dependants(db, a)
    garmin = FakeGarmin(listed=["101"], gone={"102", "104"})

    result = _syncing(db, monkeypatch, garmin).sync_garmin_activities(days_back=7)

    assert result.deleted_ids == ["deleted"]
    assert result.deleted_activities == 1
    assert result.errors == 0
    assert _stored_ids(db) == {"listed", "still_there", "old", "imported"}
    assert set(_dependants(db, "deleted").values()) == {0}
    assert set(_dependants(db, "listed").values()) == {1}  # another activity's rows stay
    # Its planned session shows as not done again (and can be matched to another activity).
    assert db.fetchall("SELECT activity_id, match_method FROM planned_sessions WHERE activity_id IS NULL") == [
        (None, None)
    ]
    # The lookup recognises a listed activity first; nothing outside the window is looked up.
    assert garmin.lookups == ["101", "102", "103"]


@pytest.mark.parametrize(
    "listed",
    [ConnectionError("Garmin is down"), []],
    ids=["list_failed", "list_empty"],
)
def test_nothing_is_removed_without_a_list(db: Database, monkeypatch, listed) -> None:
    _stored(db, "run", "201")
    garmin = FakeGarmin(listed=listed, gone={"201"})

    result = _syncing(db, monkeypatch, garmin).sync_garmin_activities(days_back=7)

    assert result.deleted_ids == []
    assert _stored_ids(db) == {"run"}
    assert garmin.lookups == []


def test_unclear_answers_keep_the_activity(db: Database, monkeypatch) -> None:
    _stored(db, "listed", "301")
    _stored(db, "unlisted", "302")

    # A failed lookup (rate limit, network) isn't "not found".
    garmin = FakeGarmin(listed=["301"], failing={"302"})
    assert _syncing(db, monkeypatch, garmin).sync_garmin_activities(days_back=7).deleted_ids == []
    assert garmin.lookups == ["301", "302"]

    # The lookup says a listed activity is gone too: it can't be trusted, so nothing is removed.
    garmin = FakeGarmin(listed=["301"], gone={"301", "302"})
    assert _syncing(db, monkeypatch, garmin).sync_garmin_activities(days_back=7).deleted_ids == []
    assert garmin.lookups == ["301"]
    assert _stored_ids(db) == {"listed", "unlisted"}


def test_not_found_is_recognised_in_either_error_form() -> None:
    from types import SimpleNamespace

    from hart.ingestion.sync_manager import _deleted_in_garmin

    class HTTPError(Exception):
        def __init__(self, status: int) -> None:
            super().__init__("Client Error")
            self.response = SimpleNamespace(status_code=status)

    class Garmin:
        def __init__(self, answer) -> None:
            self.answer = answer

        def get_activity(self, act_id: str):
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    def wrapped(status: int) -> Exception:
        try:
            raise HTTPError(status)
        except HTTPError as exc:
            try:
                raise RuntimeError("API client error") from exc
            except RuntimeError as outer:
                return outer

    assert _deleted_in_garmin(Garmin(wrapped(404)), "1") is True  # status kept on the wrapped error
    assert _deleted_in_garmin(Garmin(RuntimeError("HTTP error: API Error 404 - Not Found")), "1") is True
    assert _deleted_in_garmin(Garmin({}), "1") is True  # an empty answer
    assert _deleted_in_garmin(Garmin(wrapped(403)), "1") is None
    assert _deleted_in_garmin(Garmin(TimeoutError("read timed out")), "1") is None
    assert _deleted_in_garmin(Garmin({"activityId": 1}), "1") is False


def test_pipeline_recomputes_load_after_a_removal(tmp_path: Path) -> None:
    from hart.ingestion.sync_manager import SyncResult
    from hart.server.jobs.pipeline import run_sync_pipeline
    from hart.storage.writers import delete_activity
    from tests.test_server import FakeSyncManager

    db = Database(tmp_path / "p.duckdb").connect()
    day = datetime.date.today() - datetime.timedelta(days=1)

    def add(activity_id: str, external_id: str, sport: str, tss: float) -> None:
        db.execute(
            "INSERT INTO activities (activity_id, source, external_id, sport_type, start_time, elapsed_seconds) "
            "VALUES (?, 'garmin', ?, ?, ?, 3600)",
            [activity_id, external_id, sport, datetime.datetime.combine(day, datetime.time(7))],
        )
        db.execute(
            "INSERT INTO activity_metrics (activity_id, sport_type, date, tss) VALUES (?, ?, ?, ?)",
            [activity_id, sport, day, tss],
        )

    add("fit_ride", "401", "bike", 60)
    add("fit_swim", "402", "swim", 30)

    class Sync(FakeSyncManager):
        health_error = None
        removed: list[str] = []

        def sync_garmin_activities(self, days_back: int = 7) -> SyncResult:
            for a in self.removed:
                delete_activity(self.db, a)
            if self.removed:
                add("fit_ride", "403", "bike", 100)  # the merged upload: same start and sport as its first part
            return SyncResult(new_activities=int(bool(self.removed)), deleted_ids=list(self.removed))

    config = dataclasses.replace(get_config(), garmin=dataclasses.replace(get_config().garmin, email="x@example.com"))

    def load(sport: str) -> float | None:
        row = db.fetchone("SELECT daily_tss FROM daily_training_load WHERE date = ? AND sport_type = ?", [day, sport])
        return row[0] if row else None

    run_sync_pipeline(db, config, sync_manager_factory=Sync)
    assert (load("combined"), load("swim")) == (90, 30)

    Sync.removed = ["fit_ride", "fit_swim"]
    result = run_sync_pipeline(db, config, sync_manager_factory=Sync)

    assert result["deleted_activity_ids"] == ["fit_ride", "fit_swim"]
    assert result["steps"]["garmin_activities"]["out"]["deleted"] == 2
    assert result["new_activity_ids"] == ["fit_ride"]  # graded as the new session it is
    assert (load("combined"), load("swim")) == (100, None)  # the swim's series went with it
    assert db.fetchone("SELECT count(*) FROM daily_training_load WHERE sport_type = 'swim'")[0] == 0
    assert result["steps"]["views"]["ok"]
    db.close()
