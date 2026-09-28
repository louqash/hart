"""Tests: season calendar, observed state, readiness, pages and API."""

from __future__ import annotations

import dataclasses
import datetime
import json
from pathlib import Path
from typing import Any

import pytest

from hart.analytics.phase import generate_phases, observed_state, phase_flags
from hart.analytics.readiness import ReadinessInputs, compute_readiness
from hart.config import ServerSettings, get_config
from hart.server.state import DEFAULT_THRESHOLDS as T
from tests.conftest import write_seed

D = datetime.date


# ---------------------------------------------------------------------------
# Phase calendar
# ---------------------------------------------------------------------------


def test_phase_calendar_for_a_full_distance_race() -> None:
    phases = generate_phases(D(2027, 8, 22), D(2026, 9, 7), D(2026, 11, 8), D(2026, 9, 26), T)
    by_name = {p.name: (p.start_date, p.end_date) for p in phases}
    assert by_name["Comeback"] == (D(2026, 9, 7), D(2026, 11, 8))
    assert by_name["Base 1"][0] == D(2026, 11, 9)
    assert by_name["Base 6"] == (D(2027, 3, 29), D(2027, 4, 25))
    assert by_name["Build 1"] == (D(2027, 4, 26), D(2027, 6, 6))
    assert by_name["Build 2"] == (D(2027, 6, 7), D(2027, 7, 18))
    assert by_name["Peak"] == (D(2027, 7, 19), D(2027, 8, 8))
    assert by_name["Taper"] == (D(2027, 8, 9), D(2027, 8, 21))
    assert by_name["Race"] == (D(2027, 8, 22), D(2027, 8, 22))
    assert by_name["Transition"] == (D(2027, 8, 23), D(2027, 9, 5))
    # Contiguous, non-overlapping; every phase after the comeback starts on a Monday.
    for a, b in zip(phases, phases[1:]):
        assert b.start_date == a.end_date + datetime.timedelta(days=1)
    assert all(p.start_date.weekday() == 0 for p in phases if p.phase_type in ("base", "build", "peak", "taper"))


def test_short_runway_shrinks_base_then_build() -> None:
    phases = generate_phases(D(2027, 1, 31), None, None, D(2026, 10, 5), T)  # ~17 weeks away
    types = [p.phase_type for p in phases]
    assert "base" not in types
    build_days = sum((p.end_date - p.start_date).days + 1 for p in phases if p.phase_type == "build")
    assert build_days >= 6 * 7
    taper = next(p for p in phases if p.phase_type == "taper")
    assert (taper.end_date - taper.start_date).days + 1 >= 10


# ---------------------------------------------------------------------------
# Observed state and flags (spec–8.2.3)
# ---------------------------------------------------------------------------


def _load(days: int, ctl_start: float, ctl_step: float, tsb: float) -> list[dict[str, Any]]:
    end = D(2026, 9, 26)
    return [
        {"date": end - datetime.timedelta(days=days - 1 - i), "ctl": ctl_start + ctl_step * i, "tsb": tsb}
        for i in range(days)
    ]


def test_observed_states() -> None:
    on = D(2026, 9, 26)
    training = {on - datetime.timedelta(days=i) for i in range(0, 14, 2)}
    assert observed_state(_load(60, 20, 0.5, -5), training, on, T)["state"] == "building"
    assert observed_state(_load(60, 20, 0.0, -5), training, on, T)["state"] == "maintaining"
    assert observed_state(_load(60, 20, 0.0, -35), training, on, T)["state"] == "overreaching_risk"
    assert observed_state(_load(60, 60, -0.5, 10), set(), on, T)["state"] == "detraining"
    assert observed_state(_load(10, 20, 0.5, -5), training, on, T)["state"] == "insufficient_data"


def test_comeback_ramp_flag() -> None:
    state = {"state": "building", "ramp_7d": 5.0}
    assert phase_flags("comeback", state, [], T)
    assert not phase_flags("comeback", {**state, "ramp_7d": 3.0}, [], T)
    assert phase_flags(None, state, [], T) == ["No phase defined for today — set up your season."]


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


def test_readiness_unknown_without_data() -> None:
    result = compute_readiness(ReadinessInputs(), T)
    assert result["level"] == "unknown"
    assert "sleep" in result["missing"]


def test_readiness_green_needs_sleep_and_hrv_or_recovery() -> None:
    assert compute_readiness(ReadinessInputs(sleep_hours=7.5), T)["level"] == "unknown"
    assert compute_readiness(ReadinessInputs(sleep_hours=7.5, recovery_score=75), T)["level"] == "green"


def test_readiness_red_on_hrv_drop_only_with_baseline() -> None:
    history = [50.0] * 20
    red = compute_readiness(ReadinessInputs(sleep_hours=7.5, hrv_last_night=40, hrv_history=history), T)
    assert red["level"] == "red" and red["hits"][0]["rule"] == "hrv"
    # 10 nights of history is below the 14-night minimum: no baseline, no judgement.
    thin = compute_readiness(ReadinessInputs(sleep_hours=7.5, hrv_last_night=40, hrv_history=history[:10]), T)
    assert thin["inputs"]["hrv_baseline"] is None and thin["level"] == "unknown"


def test_readiness_amber_short_sleep_and_illness_red() -> None:
    assert compute_readiness(ReadinessInputs(sleep_hours=6.0, recovery_score=80), T)["level"] == "amber"
    assert compute_readiness(ReadinessInputs(sleep_hours=8, recovery_score=80, illness="Flu"), T)["level"] == "red"


# ---------------------------------------------------------------------------
# App: pages, API, notes, phases, MCP
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from hart.server.app import create_app
    from hart.storage.database import Database

    db_path = tmp_path / "app.duckdb"
    db = Database(db_path).connect()
    start = datetime.datetime(2026, 6, 1, 7)
    for i in range(0, 110, 2):  # a session every other day, with a gap
        if 24 <= i < 98:
            continue
        t = start + datetime.timedelta(days=i)
        db.execute(
            "INSERT INTO activities (activity_id, source, sport_type, sub_type, name, start_time, elapsed_seconds, avg_hr) "
            "VALUES (?, 'test', 'bike', 'indoor_cycling', 'Ride', ?, 3600, 130)",
            [f"a{i}", t],
        )
        db.execute(
            "INSERT INTO activity_metrics (activity_id, sport_type, date, tss, efficiency_factor, aerobic_decoupling_pct) "
            "VALUES (?, 'bike', ?, 50, 'NaN'::DOUBLE, NULL)",
            [f"a{i}", t.date()],
        )
    db.close()
    from hart.analytics.training_load import update_training_load

    db = Database(db_path).connect()
    update_training_load(db)
    db.close()

    seed = write_seed(tmp_path / "seed")
    (seed / "athlete_notes.json").write_text(
        json.dumps(
            [
                {
                    "category": "injury",
                    "title": "Shoulder",
                    "body": "Swim only Thursday.",
                    "rules": {"allowed_weekdays": {"swim": ["thu"]}},
                },
            ]
        )
    )
    config = dataclasses.replace(
        get_config(),
        db_path=db_path,
        server=ServerSettings(env="production", seed_dir=seed),
    )
    app = create_app(config, run_scheduler=False, handlers={"sync": lambda d, p: {}, "sync_light": lambda d, p: {}})
    with TestClient(app, base_url="https://hart.example.ts.net") as c:
        yield c


H = {"Tailscale-User-Login": "me@example.com"}
W = {**H, "X-Requested-With": "hart"}


def test_pages_render(client) -> None:
    for path in ("/", "/fitness", "/season", "/sessions", "/sessions/a0", "/notes", "/system"):
        resp = client.get(path, headers=H)
        assert resp.status_code == 200, (path, resp.text[:300])
    assert client.get("/sessions/nope", headers=H).status_code == 404


def test_json_endpoints_are_strict_json(client) -> None:
    for path in (
        "/api/dashboard",
        "/api/readiness",
        "/api/fitness/pmc?range=all",
        "/api/fitness/volume?weeks=26",
        "/api/fitness/efficiency",
        "/api/fitness/health",
        "/api/fitness/power-curve",
        "/api/season",
        "/api/sessions",
        "/api/sessions/a0",
        "/api/sessions/a0/streams",
        "/api/notes",
    ):
        resp = client.get(path, headers=H)
        assert resp.status_code == 200, (path, resp.text[:300])
        json.loads(resp.text, parse_constant=lambda c: pytest.fail(f"{path}: non-JSON constant {c}"))


def test_season_seeded_and_overlaps_rejected(client) -> None:
    season = client.get("/api/season", headers=H).json()
    assert any(p["phase_type"] == "race" for p in season["phases"])
    assert season["unconfirmed"] == len(season["phases"])
    race = next(p for p in season["phases"] if p["phase_type"] == "race")
    clash = client.post(
        "/api/phases",
        headers=W,
        json={"phase_type": "base", "name": "X", "start_date": race["start_date"], "end_date": race["end_date"]},
    )
    assert clash.status_code == 400 and "Overlaps" in clash.json()["error"]["message"]
    assert client.post("/api/phases/confirm", headers=W, json={}).json()["confirmed"] == season["unconfirmed"]
    # Editing a phase makes it manual; regeneration keeps it.
    first = season["phases"][0]
    client.put(f"/api/phases/{first['id']}", headers=W, json={**first, "name": "My block"})
    client.post("/api/phases/generate", headers=W, json={})
    names = [p["name"] for p in client.get("/api/season", headers=H).json()["phases"]]
    assert "My block" in names


def test_notes_lifecycle(client) -> None:
    notes = client.get("/api/notes", headers=H).json()
    seed = notes[0]
    assert seed["status"] == "proposed" and seed["rules"] == {"allowed_weekdays": {"swim": ["thu"]}}
    bad = client.put(f"/api/notes/{seed['id']}", headers=W, json={**seed, "rules": {"bogus": 1}})
    assert bad.status_code == 422
    client.put(f"/api/notes/{seed['id']}", headers=W, json={**seed, "body": "Edited"})
    client.post(f"/api/notes/{seed['id']}/approve", headers=W, json={})
    note = client.get("/api/notes", headers=H).json()[0]
    assert (note["status"], note["source"], note["body"]) == ("active", "manual", "Edited")
    client.post(f"/api/notes/{seed['id']}/archive", headers=W, json={})
    assert client.get("/api/notes?status=archived", headers=H).json()[0]["id"] == seed["id"]


def test_feedback_validation(client) -> None:
    assert client.post("/api/sessions/a0/feedback", headers=W, json={"rpe": 11}).status_code == 422
    ok = client.post("/api/sessions/a0/feedback", headers=W, json={"rpe": 6, "feel": 4, "comment": " fine "})
    assert ok.status_code == 200
    fb = client.get("/api/sessions/a0", headers=H).json()["feedback"]
    assert fb == {"rpe": 6, "feel": 4, "comment": "fine", "saved": True}


def _mcp(client, name: str, args: dict[str, Any]) -> Any:
    resp = client.post(
        "/mcp",
        headers={**H, "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}},
    )
    return json.loads(resp.json()["result"]["content"][0]["text"])


def test_mcp_athlete_context_and_proposals(client) -> None:
    ctx = _mcp(client, "get_athlete_context", {})
    assert ctx["notes"] == {} and ctx["awaiting_approval"] == ["Shoulder"]  # proposed ≠ fact
    out = _mcp(client, "propose_athlete_note", {"category": "health", "title": "Vit D low", "body": "x"})
    assert out["status"] == "proposed"
    assert _mcp(client, "propose_athlete_note", {"category": "nope", "title": "t", "body": "b"})["error"]
    phase = _mcp(client, "get_training_phase", {})
    assert "state" in phase and "flags" in phase
    assert _mcp(client, "get_readiness", {})["level"] in ("green", "amber", "red", "unknown")


def test_per_sport_ctl_uses_each_sports_latest_value(tmp_path: Path) -> None:
    import math

    from hart.server.data import per_sport_ctl
    from hart.storage.database import Database

    db = Database(tmp_path / "t.duckdb").connect()
    today = D(2026, 9, 26)
    rows = [
        ("bike", today, 10.0),
        ("run", today - datetime.timedelta(days=2), 8.0),
        ("swim", today - datetime.timedelta(days=9), 5.0),
        ("other", today, 3.0),
        ("combined", today, 20.0),
        ("strength", today - datetime.timedelta(days=300), 0.4),
    ]
    for sport, d, ctl in rows:
        db.execute("INSERT INTO daily_training_load (date, sport_type, ctl) VALUES (?, ?, ?)", [d, sport, ctl])
    out = {r["sport_type"]: r["ctl"] for r in per_sport_ctl(db, today)}
    assert out == {
        "swim": round(5.0 * math.exp(-9 / 42), 1),
        "bike": 10.0,
        "run": round(8.0 * math.exp(-2 / 42), 1),
    }  # walks ("other") excluded; long-faded strength below 0.5 dropped
    db.close()


# ---------------------------------------------------------------------------
# Season proposals from Ember
# ---------------------------------------------------------------------------


def test_season_proposals_flow(client) -> None:
    phases = client.get("/api/season", headers=H).json()["phases"]
    comeback = (
        next(p for p in phases if p["phase_type"] == "comeback")
        if any(p["phase_type"] == "comeback" for p in phases)
        else phases[0]
    )
    nxt = phases[phases.index(comeback) + 1]

    # Extending into the next phase is refused at proposal time.
    clash = _mcp(
        client,
        "propose_season_change",
        {
            "kind": "phase",
            "action": "update",
            "target_id": comeback["id"],
            "end_date": nxt["end_date"],
            "reason": "longer comeback",
        },
    )
    assert "Overlaps" in clash["error"]

    # Shrinking is fine; nothing changes until applied.
    new_end = (datetime.date.fromisoformat(comeback["end_date"]) - datetime.timedelta(days=7)).isoformat()
    prop = _mcp(
        client,
        "propose_season_change",
        {
            "kind": "phase",
            "action": "update",
            "target_id": comeback["id"],
            "end_date": new_end,
            "reason": "Shoulder cleared early",
        },
    )
    assert prop["status"] == "pending" and f"end date {comeback['end_date']} → {new_end}" in prop["summary"]
    season = client.get("/api/season", headers=H).json()
    assert season["proposals"][0]["reason"] == "Shoulder cleared early"
    assert next(p for p in season["phases"] if p["id"] == comeback["id"])["end_date"] == comeback["end_date"]

    assert client.post(f"/api/season/proposals/{prop['id']}/apply", headers=W, json={}).json()["status"] == "applied"
    changed = next(p for p in client.get("/api/season", headers=H).json()["phases"] if p["id"] == comeback["id"])
    assert changed["end_date"] == new_end and changed["source"] == "manual"
    again = client.post(f"/api/season/proposals/{prop['id']}/apply", headers=W, json={})
    assert again.status_code == 400  # already applied

    # Annotations: create, then propose a delete and dismiss it.
    ann = _mcp(
        client,
        "propose_season_change",
        {
            "kind": "annotation",
            "action": "create",
            "annotation_kind": "illness",
            "label": "Cold",
            "start_date": "2026-10-01",
            "end_date": "2026-10-05",
            "reason": "Athlete reported a cold",
        },
    )
    client.post(f"/api/season/proposals/{ann['id']}/apply", headers=W, json={})
    created = next(a for a in client.get("/api/season", headers=H).json()["annotations"] if a["label"] == "Cold")
    drop = _mcp(
        client,
        "propose_season_change",
        {"kind": "annotation", "action": "delete", "target_id": created["id"], "reason": "mistake"},
    )
    assert drop["summary"].startswith("Delete annotation “Cold”")
    client.post(f"/api/season/proposals/{drop['id']}/dismiss", headers=W, json={})
    assert any(a["label"] == "Cold" for a in client.get("/api/season", headers=H).json()["annotations"])
    assert _mcp(client, "propose_season_change", {"kind": "phase", "action": "create", "reason": ""})["error"]


def test_every_tip_has_a_glossary_entry(client) -> None:
    import re

    from hart.server.glossary import GLOSSARY

    for path in ("/", "/fitness", "/season", "/sessions/a0"):
        html = client.get(path, headers=H).text
        tips = re.findall(r'class="tip"[^>]*data-tip="([^"]*)"', html)
        assert tips, path
        assert all(t.strip() for t in tips), f"empty tip on {path}"
    assert all(len(v) > 20 for v in GLOSSARY.values())


def test_unknown_readiness_explains_itself() -> None:
    nothing = compute_readiness(ReadinessInputs(), T)
    assert [m["input"] for m in nothing["missing_detail"]] == ["Sleep", "Recovery score"]
    no_hrv = compute_readiness(ReadinessInputs(sleep_hours=7.5), T)
    assert no_hrv["level"] == "unknown" and no_hrv["missing_detail"][0]["input"] == "HRV"
    short = compute_readiness(ReadinessInputs(sleep_hours=7.5, hrv_last_night=60, hrv_history=[58] * 5), T)
    detail = short["missing_detail"][0]
    assert detail["input"] == "HRV baseline" and "you have 5" in detail["why"] and "9 more night" in detail["fix"]
