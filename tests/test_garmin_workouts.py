"""Sending planned sessions to Garmin Connect as structured workouts."""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from tests.test_chat import FakeClient, _result
from tests.test_grading import H, W, _wait_job
from tests.test_plan import _regenerate, _s, _suggestion, client  # noqa: F401 — pytest fixture
from hart.server import garmin_workouts as gw

INTERVALS = {
    "name": "3x5' @200W",
    "blocks": [
        {"steps": [{"kind": "warmup", "minutes": 10, "target": "power", "power_low": 100, "power_high": 150}]},
        {"repeat": 3, "steps": [
            {"kind": "interval", "minutes": 5, "target": "power", "power_low": 195, "power_high": 205,
             "cadence_low": 80, "cadence_high": 110},
            {"kind": "recovery", "minutes": 2, "target": "power", "power_low": 145, "power_high": 155}]},
        {"steps": [{"kind": "cooldown", "minutes": 9}]},
    ],
}
EASY_RUN = {"name": "Z2 run 30'", "blocks": [{"steps": [{"kind": "interval", "minutes": 30, "target": "hr_zone", "hr_zone": 2}]}]}


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    FakeClient.instances.clear()
    FakeClient.scripts.clear()


def test_build_bike_workout_with_repeats_and_cadence() -> None:
    from garminconnect.workout import CyclingWorkout

    w = gw.build_workout("bike", gw.WorkoutSteps.model_validate(INTERVALS), "coach text")
    CyclingWorkout.model_validate(w)  # the library's own model accepts it
    steps = w["workoutSegments"][0]["workoutSteps"]
    assert [s["stepOrder"] for s in steps] == [1, 2, 5]
    group = steps[1]
    assert group["type"] == "RepeatGroupDTO" and group["numberOfIterations"] == 3
    work = group["workoutSteps"][0]
    assert work["childStepId"] == 1 and work["targetValueOne"] == 195 and work["endConditionValue"] == 300
    assert work["secondaryTargetType"]["workoutTargetTypeKey"] == "cadence" and work["secondaryTargetValueTwo"] == 110
    assert steps[2]["targetType"]["workoutTargetTypeKey"] == "no.target"
    assert w["estimatedDurationInSecs"] == 40 * 60


def test_build_run_with_hr_zone() -> None:
    w = gw.build_workout("run", gw.WorkoutSteps.model_validate(EASY_RUN))
    step = w["workoutSegments"][0]["workoutSteps"][0]
    assert w["sportType"]["sportTypeKey"] == "running"
    assert step["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone" and step["zoneNumber"] == 2
    with pytest.raises(gw.GarminError):
        gw.build_workout("strength", gw.WorkoutSteps.model_validate(EASY_RUN))


def test_step_validation() -> None:
    with pytest.raises(ValueError):
        gw.Step(kind="interval", minutes=5, target="power", power_low=200)
    with pytest.raises(ValueError):
        gw.Step(kind="interval", minutes=5, cadence_low=80)


class FakeGarmin:
    def __init__(self) -> None:
        self.uploaded: list[dict[str, Any]] = []
        self.scheduled: list[tuple[Any, str]] = []
        self.deleted: list[str] = []

    def upload_workout(self, workout: dict[str, Any]) -> dict[str, Any]:
        self.uploaded.append(workout)
        return {"workoutId": 1000 + len(self.uploaded)}

    def schedule_workout(self, workout_id: Any, date_str: str) -> dict[str, Any]:
        self.scheduled.append((workout_id, date_str))
        return {"workoutScheduleId": 5000 + len(self.scheduled)}

    def delete_workout(self, workout_id: Any) -> None:
        self.deleted.append(str(workout_id))


@pytest.fixture
def garmin(monkeypatch) -> FakeGarmin:
    fake = FakeGarmin()
    monkeypatch.setattr(gw, "_client", lambda db, config: fake)
    return fake


def _plan(client, **kw: Any) -> int:
    body = {"date": str(datetime.date.today() + datetime.timedelta(days=1)), "sport_type": "run", "title": "Easy run",
            "duration_min": 30, "intensity": "endurance", "description": "30' Z2", **kw}
    return client.post("/api/plan", headers=W, json=body).json()["id"]


def _send(client, planned_id: int) -> dict[str, Any]:
    return _wait_job(client, client.post(f"/api/plan/{planned_id}/send-garmin", headers=W, json={}).json()["job_id"])


def test_send_resend_and_delete(client, garmin) -> None:
    planned_id = _plan(client)
    FakeClient.scripts.extend([[_result(structured_output=EASY_RUN)], [_result(structured_output=EASY_RUN)]])
    job = _send(client, planned_id)
    assert job["status"] == "ok" and garmin.scheduled == [(1001, str(datetime.date.today() + datetime.timedelta(days=1)))]
    assert FakeClient.instances[0].options.model == "claude-sonnet-5"
    row = client.get("/api/plan", headers=H).json()["items"][0]
    assert row["garmin_status"] == "sent" and row["garmin_workout_id"] == "1001"
    # Re-sending replaces the old workout.
    _send(client, planned_id)
    assert garmin.deleted == ["1001"] and len(garmin.uploaded) == 2
    # Editing marks it outdated; deleting the row deletes it on Garmin.
    client.put(f"/api/plan/{planned_id}", headers=W, json={**row, "duration_min": 35, "description": "35' Z2"})
    assert client.get("/api/plan", headers=H).json()["items"][0]["garmin_status"] == "outdated"
    out = client.request("DELETE", f"/api/plan/{planned_id}", headers=W, json={}).json()
    _wait_job(client, out["garmin"]["job_id"])
    assert garmin.deleted == ["1001", "1002"]


def test_duration_mismatch_fails_without_upload(client, garmin) -> None:
    planned_id = _plan(client, duration_min=90)
    FakeClient.scripts.append([_result(structured_output=EASY_RUN)])  # 30 min of steps for a 90 min session
    job = _send(client, planned_id)
    assert job["status"] == "error" and "add up to 30 min" in job["error"] and garmin.uploaded == []
    assert client.get("/api/plan", headers=H).json()["items"][0]["garmin_status"] == "failed"


def test_strength_is_not_sendable(client, garmin) -> None:
    planned_id = _plan(client, sport_type="strength", title="Session B")
    assert client.post(f"/api/plan/{planned_id}/send-garmin", headers=W, json={}).status_code == 400


def test_accept_sends_to_garmin(client, garmin) -> None:
    today = datetime.date.today()
    FakeClient.scripts.append([_result(structured_output=_suggestion("free_choice", [_s("run", 30)]))])
    _regenerate(client, today)
    s = client.get(f"/api/suggestions/{today}", headers=H).json()
    FakeClient.scripts.append([_result(structured_output=EASY_RUN)])
    out = client.post(f"/api/suggestions/{s['id']}/accept", headers=W, json={}).json()
    assert len(out["garmin"]) == 1
    _wait_job(client, out["garmin"][0]["job_id"])
    assert garmin.scheduled and garmin.scheduled[0][1] == str(today)
    # Auto-send can be switched off.
    client.put("/api/garmin/auto-send", headers=W, json={"enabled": False})
    FakeClient.scripts.append([_result(structured_output=_suggestion("free_choice", [_s("run", 30)]))])
    _regenerate(client, today)
    s2 = client.get(f"/api/suggestions/{today}", headers=H).json()
    assert client.post(f"/api/suggestions/{s2['id']}/accept", headers=W, json={}).json()["garmin"] == []
