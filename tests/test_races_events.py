"""Adding and editing races and events (annotations), by hand and via Ember proposals."""

from __future__ import annotations

import dataclasses
import datetime
import json
from pathlib import Path

import pytest

from hart.config import ServerSettings, get_config
from tests.conftest import write_seed
from tests.test_grading import H, W

FUTURE = datetime.date.today() + datetime.timedelta(days=200)


@pytest.fixture
def client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from hart.server.app import create_app

    config = dataclasses.replace(
        get_config(),
        db_path=tmp_path / "r.duckdb",
        server=ServerSettings(env="production", seed_dir=write_seed(tmp_path / "seed")),
    )
    app = create_app(config, run_scheduler=False)
    with TestClient(app, base_url="https://hart.example.ts.net") as c:
        yield c


def _races(client) -> list[dict]:
    return client.get("/api/season", headers=H).json()["races"]


def test_race_crud_and_phase_signal(client) -> None:
    seeded = _races(client)
    assert [r["name"] for r in seeded] == ["Example Ironman"]
    # A B-race doesn't change the phase plan; an earlier A-race does.
    b = client.post(
        "/api/races",
        headers=W,
        json={"name": "Spring Half", "race_date": str(FUTURE), "distance": "half", "priority": "B"},
    ).json()
    assert b["phases_stale"] is False
    a = client.put(
        f"/api/races/{b['id']}",
        headers=W,
        json={"name": "Spring Half", "race_date": str(FUTURE), "distance": "half", "priority": "A", "notes": "tune-up"},
    ).json()
    assert a["phases_stale"] is True
    assert next(r for r in _races(client) if r["id"] == b["id"])["notes"] == "tune-up"
    assert client.request("DELETE", f"/api/races/{b['id']}", headers=W, json={}).json()["phases_stale"] is True
    assert client.request("DELETE", "/api/races/9999", headers=W, json={}).status_code == 404
    bad = client.post(
        "/api/races", headers=W, json={"name": "X", "race_date": str(FUTURE), "distance": "ultra", "priority": "B"}
    )
    assert bad.status_code == 422
    assert "Edit" in client.get("/season", headers=H).text


def test_annotation_edit(client) -> None:
    created = client.post(
        "/api/annotations",
        headers=W,
        json={"kind": "event", "label": "Camp", "start_date": "2026-10-01", "end_date": "2026-10-05"},
    ).json()
    client.put(
        f"/api/annotations/{created['id']}",
        headers=W,
        json={"kind": "travel", "label": "Camp Mallorca", "start_date": "2026-10-01", "end_date": "2026-10-08"},
    )
    ann = next(a for a in client.get("/api/season", headers=H).json()["annotations"] if a["id"] == created["id"])
    assert (ann["kind"], ann["label"], ann["end_date"]) == ("travel", "Camp Mallorca", "2026-10-08")
    assert (
        client.put(
            "/api/annotations/9999", headers=W, json={"kind": "event", "label": "x", "start_date": "2026-10-01"}
        ).status_code
        == 404
    )


def test_guide_can_propose_races(client) -> None:
    from hart import mcp_server

    out = json.loads(
        mcp_server.propose_season_change(
            kind="race",
            action="create",
            reason="Signed up yesterday",
            name="Susz Olympic",
            race_date=str(FUTURE),
            distance="olympic",
            priority="C",
        )
    )
    assert out["status"] == "pending" and "Susz Olympic" in out["summary"]
    applied = client.post(f"/api/season/proposals/{out['id']}/apply", headers=W, json={}).json()
    assert applied["status"] == "applied" and applied["phases_stale"] is False
    race = next(r for r in _races(client) if r["name"] == "Susz Olympic")
    move = json.loads(
        mcp_server.propose_season_change(
            kind="race",
            action="update",
            reason="Date moved",
            target_id=race["id"],
            race_date=str(FUTURE + datetime.timedelta(days=7)),
        )
    )
    assert "race date" in move["summary"]
    bad = json.loads(
        mcp_server.propose_season_change(
            kind="race", action="create", reason="x", name="Y", race_date=str(FUTURE), distance="marathon", priority="B"
        )
    )
    assert "distance must be" in bad["error"]


def test_deleted_races_stay_deleted(tmp_path: Path) -> None:
    from hart.server.seed import seed_annotations, seed_races
    from hart.storage.database import Database

    db = Database(tmp_path / "s.duckdb").connect()
    seed_dir = write_seed(tmp_path / "seed")
    assert seed_races(db, seed_dir) == 1
    db.execute("DELETE FROM races")
    assert seed_races(db, seed_dir) == 0  # a restart doesn't bring the seeded race back
    seed_annotations(db, seed_dir)
    db.execute("DELETE FROM annotations")
    assert seed_annotations(db, seed_dir)["inserted"] == 0
    db.close()


def test_canopy_shows_next_race_and_countdown(client) -> None:
    today = datetime.date.today()
    client.post(
        "/api/races",
        headers=W,
        json={
            "name": "Spring Half",
            "race_date": str(today + datetime.timedelta(days=1)),
            "distance": "half",
            "priority": "B",
        },
    )
    d = client.get("/api/dashboard", headers=H).json()
    assert d["race"]["name"] == "Example Ironman" and d["next_race"]["name"] == "Spring Half"
    page = client.get("/", headers=H).text
    assert "Next up:" in page and "tomorrow" in page and "weeks" in page
    for r in _races(client):
        client.request("DELETE", f"/api/races/{r['id']}", headers=W, json={})
    page = client.get("/", headers=H).text
    assert "No A-race set" in page and "Add your main race" in page
