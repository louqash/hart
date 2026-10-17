"""Sessions recorded in parts: which recordings belong together, the joined numbers, joining in the database,
the sync (auto-join, skipping joined recordings, join-aware re-reads), splitting back, and the session page."""

from __future__ import annotations

import dataclasses
import datetime
import json
from pathlib import Path
from typing import Any

import pytest

from hart.analytics.joins import combine_activity, combine_metrics, join_chains, join_problem
from hart.config import ServerSettings, get_config
from hart.ingestion.joins import JoinError, garmin_ids, join_activities, joined_garmin_ids
from hart.ingestion.sync_manager import SyncManager, SyncResult
from hart.storage.database import Database
from tests.test_grading import H, W, _wait_job

T0 = datetime.datetime.combine(datetime.date.today(), datetime.time(6, 0))  # recent, and the day stays the same


def _at(minutes: float) -> datetime.datetime:
    return T0 + datetime.timedelta(minutes=minutes)


def _a(activity_id: str, start_min: float, minutes: float, sport: str = "bike", sub: str = "road_biking", **kw: Any):
    return {
        "activity_id": activity_id,
        "sport_type": sport,
        "sub_type": sub,
        "start_time": _at(start_min),
        "elapsed_seconds": int(minutes * 60),
        **kw,
    }


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "j.duckdb").connect()
    yield database
    database.close()


def _garmin_config() -> Any:
    base = get_config()
    return dataclasses.replace(base, garmin=dataclasses.replace(base.garmin, email="x@example.com"))


# ---------------------------------------------------------------------------
# Which recordings belong together
# ---------------------------------------------------------------------------


def test_chains_join_consecutive_recordings_of_one_kind_within_the_gap() -> None:
    acts = [
        _a("ride1", 0, 40),
        _a("ride2", 43, 50),  # 3 min after ride1 ended
        _a("ride3", 95, 20),  # 2 min after ride2
        _a("run", 120, 15, sport="run", sub="road_running"),  # a brick: another sport
        _a("ride4", 140, 30),  # the run was in between
    ]
    assert join_chains(acts, 10) == [["ride1", "ride2", "ride3"]]
    assert join_chains(acts, 0) == []  # off


def test_chains_keep_apart_long_gaps_indoor_and_outdoor_and_different_other_kinds() -> None:
    assert join_chains([_a("a", 0, 30), _a("b", 45, 30)], 10) == []  # a 15-min gap
    assert join_chains([_a("trainer", 0, 30, sub="indoor_cycling"), _a("road", 32, 30)], 10) == []
    walk, yoga = _a("walk", 0, 30, sport="other", sub="walking"), _a("yoga", 31, 30, sport="other", sub="yoga")
    assert join_chains([walk, yoga], 10) == []
    assert join_chains([walk, _a("walk2", 31, 20, sport="other", sub="walking")], 10) == [["walk", "walk2"]]


def test_join_problem() -> None:
    assert join_problem([_a("a", 0, 30)]) is not None
    assert "same sport" in join_problem([_a("a", 0, 30), _a("b", 31, 20, sport="run", sub="road_running")])
    assert "overlap" in join_problem([_a("a", 0, 30), _a("b", 20, 20)])
    assert join_problem([_a("a", 0, 30), _a("b", 200, 20)]) is None  # by hand, any gap


# ---------------------------------------------------------------------------
# The joined numbers
# ---------------------------------------------------------------------------


def test_combined_activity_adds_up_and_weights_averages_by_moving_time() -> None:
    first = _a(
        "p1", 0, 62, moving_seconds=3600, distance_meters=30000, total_elevation_m=200, avg_hr=140, max_hr=165,
        avg_power=200, max_power=600, normalized_power=210, avg_cadence=88, avg_speed_kmh=30, calories=700,
        garmin_training_load=60.0, training_effect_aerobic=2.8, training_effect_label="BASE", rpe=None, feel=None,
        description="Legs fresh", begin_stamina=95, end_stamina=70, name="Morning ride",
    )  # fmt: skip
    second = _a(
        "p2", 65, 31, moving_seconds=1800, distance_meters=15000, total_elevation_m=100, avg_hr=152, max_hr=171,
        avg_power=230, max_power=700, normalized_power=250, avg_cadence=85, avg_speed_kmh=30, calories=400,
        garmin_training_load=45.0, training_effect_aerobic=3.4, training_effect_label="TEMPO", rpe=60, feel=50,
        description="Pushed the last climb", begin_stamina=68, end_stamina=55, name="Ride",
    )  # fmt: skip
    j = combine_activity([first, second])
    assert j["activity_id"] == "p1" and j["name"] == "Morning ride"
    assert j["elapsed_seconds"] == 96 * 60  # first start to last end, the gap counts like a pause
    assert j["moving_seconds"] == 5400 and j["distance_meters"] == 45000 and j["total_elevation_m"] == 300
    assert j["avg_hr"] == 144  # (140·3600 + 152·1800) / 5400
    assert j["avg_power"] == 210 and j["max_power"] == 700 and j["max_hr"] == 171
    assert j["normalized_power"] == round(((210**4 * 2 + 250**4) / 3) ** 0.25)
    assert j["avg_speed_kmh"] == 30.0 and j["calories"] == 1100 and j["garmin_training_load"] == 105.0
    assert j["training_effect_aerobic"] == 3.4 and j["training_effect_label"] == "TEMPO"
    assert (j["rpe"], j["feel"]) == (60, 50)  # the effort given at the end
    assert j["description"] == "Legs fresh\n\nPushed the last climb"
    assert (j["begin_stamina"], j["end_stamina"]) == (95, 55)


def test_combined_metrics() -> None:
    parts = [
        _a("p1", 0, 60, moving_seconds=3600, sport="run", sub="road_running"),
        _a("p2", 62, 30, moving_seconds=1800, sport="run", sub="road_running"),
    ]
    joined = {**parts[0], "avg_hr": 150, "distance_meters": 15000, "moving_seconds": 5400, "normalized_power": None}
    m = combine_metrics(
        parts,
        [
            {"tss": 50.0, "tss_method": "garmin", "hr_zone_seconds": {"Z2": 3000, "Z3": 600}, "estimated_kj": None,
             "avg_ground_contact_ms": 240.0},
            {"tss": 30.0, "tss_method": "garmin", "hr_zone_seconds": {"Z3": 1500, "Z4": 300},
             "avg_ground_contact_ms": 255.0},
        ],
        joined,
    )  # fmt: skip
    assert m["tss"] == 80.0 and m["tss_method"] == "garmin"
    assert m["hr_zone_seconds"] == {"Z2": 3000, "Z3": 2100, "Z4": 300}
    assert m["efficiency_factor"] == round(15000 / 5400 / (150 / 60), 4)
    assert m["avg_ground_contact_ms"] == 245.0


# ---------------------------------------------------------------------------
# Joining in the database
# ---------------------------------------------------------------------------


def _store(db: Database, a: dict[str, Any], *, external_id: str | None = None, laps: int = 2, n: int = 120) -> None:
    db.execute(
        "INSERT INTO activities (activity_id, source, external_id, sport_type, sub_type, name, description, "
        "start_time, elapsed_seconds, moving_seconds, distance_meters, avg_hr, avg_power, normalized_power, "
        "garmin_training_load) VALUES (?, 'garmin', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [a["activity_id"], external_id, a["sport_type"], a["sub_type"], a.get("name", "Ride"), a.get("description"),
         a["start_time"], a["elapsed_seconds"], a["elapsed_seconds"], 10000, 140, 200, 205, 40.0],
    )  # fmt: skip
    epoch = int(a["start_time"].timestamp())
    db.executemany(
        "INSERT INTO activity_streams (activity_id, timestamp_sec, heart_rate, power) VALUES (?, ?, 140, 200)",
        [[a["activity_id"], epoch + i] for i in range(n)],
    )
    db.executemany(
        "INSERT INTO activity_laps (activity_id, lap_index, elapsed_seconds) VALUES (?, ?, 600)",
        [[a["activity_id"], i] for i in range(laps)],
    )
    db.executemany(
        "INSERT INTO hrv_samples (activity_id, sample_index, rr_interval_ms) VALUES (?, ?, 430)",
        [[a["activity_id"], i] for i in range(3)],
    )
    db.execute(
        "INSERT INTO activity_metrics (activity_id, sport_type, date, tss, tss_method, hr_zone_seconds) "
        "VALUES (?, ?, ?, 40, 'garmin', ?)",
        [a["activity_id"], a["sport_type"], a["start_time"].date(), json.dumps({"Z2": a["elapsed_seconds"]})],
    )


def test_join_moves_everything_into_the_first_recording(db: Database) -> None:
    first, second = _a("p1", 0, 40, description="Easy start"), _a("p2", 43, 30, description="Chain dropped")
    _store(db, first, external_id="9001")
    _store(db, second, external_id="9002", laps=3)
    db.execute(
        "INSERT INTO session_feedback (activity_id, rpe, comment) VALUES ('p2', 6, 'Felt strong after the stop')"
    )
    db.execute("INSERT INTO session_grades (activity_id, version, status) VALUES ('p2', 1, 'graded')")
    db.execute(
        "INSERT INTO planned_sessions (date, sport_type, title, source, activity_id, match_method) "
        "VALUES (?, 'bike', 'Endurance', 'coach_import', 'p2', 'manual')",
        [T0.date()],
    )

    out = join_activities(db, ["p2", "p1"], "manual")

    assert out == {"activity_id": "p1", "parts": ["p2"]}
    assert db.fetchall("SELECT activity_id FROM activities") == [("p1",)]
    a = db.fetchone("SELECT elapsed_seconds, moving_seconds, distance_meters, garmin_training_load, description "
                    "FROM activities WHERE activity_id = 'p1'")  # fmt: skip
    assert a == (73 * 60, 70 * 60, 20000, 80.0, "Easy start\n\nChain dropped")
    assert db.fetchone("SELECT count(*) FROM activity_streams WHERE activity_id = 'p1'")[0] == 240
    assert [r[0] for r in db.fetchall("SELECT lap_index FROM activity_laps ORDER BY lap_index")] == [0, 1, 2, 3, 4]
    assert db.fetchone("SELECT count(*), max(sample_index) FROM hrv_samples WHERE activity_id = 'p1'") == (6, 5)
    tss, zones = db.fetchone("SELECT tss, hr_zone_seconds FROM activity_metrics WHERE activity_id = 'p1'")
    assert tss == 80.0 and json.loads(zones) == {"Z2": 70 * 60}
    # The note and effort from the second recording; its grade is gone; the plan link moved.
    assert db.fetchone("SELECT rpe, comment FROM session_feedback WHERE activity_id = 'p1'") == (
        6,
        "Easy start\n\nFelt strong after the stop",
    )
    for table in ("activity_streams", "activity_laps", "hrv_samples", "activity_metrics", "session_grades",
                  "session_feedback"):  # fmt: skip
        assert db.fetchone(f"SELECT count(*) FROM {table} WHERE activity_id = 'p2'")[0] == 0, table
    assert db.fetchone("SELECT activity_id, match_method FROM planned_sessions") == ("p1", "manual")
    assert joined_garmin_ids(db) == {"9002"}
    assert garmin_ids(db, "p1") == ["9001", "9002"]


def test_a_third_recording_joins_an_already_joined_session(db: Database) -> None:
    for i, (start, minutes) in enumerate(((0, 30), (32, 30), (64, 20)), start=1):
        _store(db, _a(f"p{i}", start, minutes), external_id=f"90{i}")
    join_activities(db, ["p1", "p2"], "auto")
    join_activities(db, ["p1", "p3"], "auto")
    assert garmin_ids(db, "p1") == ["901", "902", "903"]
    assert db.fetchone("SELECT elapsed_seconds FROM activities WHERE activity_id = 'p1'")[0] == 84 * 60


def test_join_refuses_other_sports(db: Database) -> None:
    _store(db, _a("ride", 0, 30))
    _store(db, _a("run", 35, 30, sport="run", sub="road_running"))
    with pytest.raises(JoinError, match="same sport"):
        join_activities(db, ["ride", "run"], "manual")
    assert db.fetchone("SELECT count(*) FROM activities")[0] == 2


# ---------------------------------------------------------------------------
# The sync
# ---------------------------------------------------------------------------


class _Garmin:
    """Garmin Connect as the sync sees it: an activity list, details and strength sets."""

    def __init__(self, listed: list[dict[str, Any]], details: dict[str, dict[str, Any]] | None = None) -> None:
        self.listed, self.details, self.downloads = listed, details or {}, []

    def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]:
        return self.listed

    def get_activity(self, act_id: str) -> dict[str, Any]:
        return self.details.get(str(act_id), {})

    def download_activity(self, act_id: str, dl_fmt: Any = None) -> bytes:
        self.downloads.append(str(act_id))
        return b""


def test_sync_doesnt_bring_back_a_joined_recording_and_keeps_the_joined_description(db, monkeypatch) -> None:
    _store(db, _a("p1", 0, 40, description="Easy start"), external_id="9001")
    _store(db, _a("p2", 43, 30, description="Chain dropped"), external_id="9002")
    join_activities(db, ["p1", "p2"], "auto")
    garmin = _Garmin(
        [
            {"activityId": 9001, "activityName": "Ride", "description": "Easy start"},
            {"activityId": 9002, "activityName": "Ride", "description": "Chain dropped"},
        ]
    )
    manager = SyncManager(db, _garmin_config())
    monkeypatch.setattr(manager, "get_garmin_client", lambda: garmin)
    result = manager.sync_garmin_activities(days_back=2)
    assert garmin.downloads == [] and result.new_activities == 0
    assert result.updated_activities == 0 and result.described_ids == []  # unchanged: no regrade
    garmin.listed[1]["description"] = "Chain dropped, fixed it"
    result = manager.sync_garmin_activities(days_back=2)
    assert result.described_ids == ["p1"]
    assert db.fetchone("SELECT description FROM activities")[0] == "Easy start\n\nChain dropped, fixed it"


def test_a_joined_session_deleted_in_garmin_lets_its_other_recordings_come_back(db, monkeypatch) -> None:
    _store(db, _a("p1", 0, 40), external_id="9001")
    _store(db, _a("p2", 43, 30), external_id="9002")
    join_activities(db, ["p1", "p2"], "auto")
    # The first recording was deleted in Garmin Connect; the second one is still there.
    garmin = _Garmin([{"activityId": 9002, "activityName": "Ride"}], {"9002": {"activityName": "Ride"}})
    manager = SyncManager(db, _garmin_config())
    monkeypatch.setattr(manager, "get_garmin_client", lambda: garmin)
    result = manager.sync_garmin_activities(days_back=2)
    assert result.deleted_ids == ["p1"]
    assert db.fetchone("SELECT count(*) FROM activity_joins")[0] == 0
    assert garmin.downloads == ["9002"]  # downloaded again as an activity of its own


def test_effort_and_strength_sets_of_a_joined_session_come_from_all_its_recordings(db, monkeypatch) -> None:
    from hart.ingestion import sync_manager as sm
    from hart.models.activity import StrengthSet

    for activity_id, start, ext in (("g1", 0, "71"), ("g2", 25, "72")):
        _store(db, _a(activity_id, start, 20, sport="strength", sub="strength_training"), external_id=ext)
    join_activities(db, ["g1", "g2"], "auto")
    details = {
        "71": {"summaryDTO": {"directWorkoutRpe": 50, "directWorkoutFeel": None}},
        "72": {"summaryDTO": {"directWorkoutRpe": 70, "directWorkoutFeel": 75}},
    }
    manager = SyncManager(db, _garmin_config())
    monkeypatch.setattr(manager, "get_garmin_client", lambda: _Garmin([], details))
    assert manager.refresh_recent_effort(days=2) == ["g1"]
    assert db.fetchone("SELECT rpe, feel FROM activities WHERE activity_id = 'g1'") == (70, 75)

    def sets(_garmin: Any, act_id: str) -> list[StrengthSet]:
        name = {"71": "SQUAT", "72": "DEADLIFT"}[act_id]
        return [StrengthSet(set_index=i, set_type="active", repetitions=5, exercise_name=name) for i in range(2)]

    monkeypatch.setattr(sm, "_fetch_strength_sets", sets)
    assert manager.refresh_recent_strength_sets(days=2) == ["g1"]
    assert db.fetchall("SELECT set_index, exercise_name FROM strength_sets ORDER BY set_index") == [
        (0, "SQUAT"), (1, "SQUAT"), (2, "DEADLIFT"), (3, "DEADLIFT"),
    ]  # fmt: skip


def test_pipeline_joins_a_restarted_session_before_grading(db) -> None:
    from hart.server.jobs.pipeline import run_sync_pipeline
    from tests.test_server import FakeSyncManager

    class Restarted(FakeSyncManager):
        def sync_garmin_activities(self, days_back: int = 7) -> SyncResult:
            _store(self.db, _a("r1", 0, 35), external_id="501")
            _store(self.db, _a("r2", 38, 50), external_id="502")
            return SyncResult(new_activities=2)

    result = run_sync_pipeline(db, _garmin_config(), sync_manager_factory=Restarted)
    assert result["new_activity_ids"] == ["r1"]
    assert result["joined"] == [{"activity_id": "r1", "parts": ["r2"], "waited": False}]
    assert result["steps"]["joins"]["out"] == {"joined": 1}
    load = db.fetchone(
        "SELECT daily_tss FROM daily_training_load WHERE sport_type = 'combined' AND date = ?", [T0.date()]
    )
    assert load[0] == 80.0  # both parts' load, counted once


def test_grading_after_a_join(db) -> None:
    from hart.server.joins import grade_after

    calls: list[tuple[str, dict[str, Any]]] = []

    def enqueue(job_type: str, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        calls.append((job_type, payload))
        return {"job_id": 1, "status": "queued"}

    _store(db, _a("x1", 0, 30))
    assert grade_after(db, None, {"activity_id": "x1"}, enqueue, ["x1"]) is None  # new: the sync grades it
    db.execute("INSERT INTO session_grades (activity_id, version, status) VALUES ('x1', 1, 'graded')")
    assert grade_after(db, None, {"activity_id": "x1"}, enqueue) == "grading"
    assert calls == [("grade", {"activity_id": "x1", "trigger": "joined"})]


# ---------------------------------------------------------------------------
# Splitting back
# ---------------------------------------------------------------------------


def test_split_back_downloads_the_recordings_again_and_keeps_them_apart(db, monkeypatch) -> None:
    from hart.ingestion import sync_manager as sm
    from hart.server.joins import auto_join

    first, second = _a("s1", 0, 40), _a("s2", 42, 30)
    _store(db, first, external_id="801")
    _store(db, second, external_id="802")
    db.execute("INSERT INTO session_feedback (activity_id, rpe) VALUES ('s1', 5)")
    join_activities(db, ["s1", "s2"], "auto")

    garmin = _Garmin([], {"801": {"activityName": "Ride"}, "802": {"activityName": "Ride", "description": "Part 2"}})
    manager = SyncManager(db, _garmin_config())
    monkeypatch.setattr(manager, "get_garmin_client", lambda: garmin)
    monkeypatch.setattr(sm, "_download_fit", lambda g, act_id: b"FIT" + act_id.encode())
    monkeypatch.setattr(sm, "_parse_fit", lambda parser, data: None)
    originals = {"801": first, "802": second}

    def reimport(garmin: Any, parser: Any, act: dict[str, Any], fit_data: bytes) -> str:
        a = originals[act["activityId"]]
        if a["activity_id"] == "s1":
            db.execute("UPDATE activities SET elapsed_seconds = ? WHERE activity_id = 's1'", [a["elapsed_seconds"]])
        else:
            _store(db, {**a, "description": act.get("description")}, external_id=act["activityId"])
        return a["activity_id"]

    monkeypatch.setattr(manager, "_import_activity", reimport)
    assert manager.split_back("s1") == {"activity_id": "s1", "activity_ids": ["s1", "s2"]}
    assert db.fetchone("SELECT count(*) FROM activity_streams WHERE activity_id = 's1'")[0] == 0  # cleared
    assert db.fetchone("SELECT rpe FROM session_feedback WHERE activity_id = 's1'")[0] == 5  # the note stays
    assert joined_garmin_ids(db) == set() and garmin_ids(db, "s1") == ["801"]
    assert auto_join(db, None, T0.date()) == []  # never joined automatically again
    with pytest.raises(JoinError, match="wasn't joined"):
        manager.split_back("s1")


def test_split_back_changes_nothing_when_a_download_fails(db, monkeypatch) -> None:
    from hart.ingestion import sync_manager as sm

    _store(db, _a("s1", 0, 40), external_id="801")
    _store(db, _a("s2", 42, 30), external_id="802")
    join_activities(db, ["s1", "s2"], "auto")
    manager = SyncManager(db, _garmin_config())
    monkeypatch.setattr(manager, "get_garmin_client", lambda: _Garmin([]))
    monkeypatch.setattr(sm, "_download_fit", lambda g, act_id: None)
    with pytest.raises(RuntimeError):
        manager.split_back("s1")
    assert garmin_ids(db, "s1") == ["801", "802"]
    assert db.fetchone("SELECT count(*) FROM activity_streams WHERE activity_id = 's1'")[0] == 240


# ---------------------------------------------------------------------------
# The session page
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from hart.server.app import create_app

    db_path = tmp_path / "app.duckdb"
    seed = Database(db_path).connect()
    _store(seed, _a("w1", 0, 40, name="Tempo ride"), external_id="601")
    _store(seed, _a("w2", 44, 25, name="Ride"), external_id="602")
    _store(seed, _a("w3", 90, 30, sport="run", sub="road_running", name="Run off the bike"))
    seed.close()
    config = dataclasses.replace(
        get_config(), db_path=db_path, server=ServerSettings(env="production", seed_dir=tmp_path / "seed")
    )
    app = create_app(config, run_scheduler=False)
    with TestClient(app, base_url="https://hart.example.ts.net") as c:
        yield c


def test_join_from_the_session_page(client) -> None:
    page = client.get("/sessions/w2", headers=H).text
    assert "Tempo ride" in page and "ended 4 min before this one started" in page and "Join them" in page
    assert "Join them" not in client.get("/sessions/w3", headers=H).text  # nothing to join a run with

    refused = client.post("/api/sessions/w2/join", headers=W, json={"other": "w3"})
    assert refused.status_code == 400 and "same sport" in refused.json()["error"]["message"]

    job = client.post("/api/sessions/w2/join", headers=W, json={"other": "w1"}).json()
    assert job["activity_id"] == "w1"
    assert _wait_job(client, job["job_id"])["status"] == "ok"
    page = client.get("/sessions/w1", headers=H).text
    assert "One session from 2 recordings" in page and "Split back" in page
    assert client.get("/sessions/w2", headers=H).status_code == 404
