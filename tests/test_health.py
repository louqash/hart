"""Tests: marker catalogue, reminder rules, health checks, lab paste import."""

from __future__ import annotations

import dataclasses
import datetime
import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_chat import FakeClient, _result
from tests.test_grading import H, W
from hart.analytics.health_checks import DEFAULTS, PANEL, Context, evaluate, last_full_panel
from hart.config import ServerSettings, get_config
from hart.server import health, labs
from hart.storage.database import Database

D = datetime.date
TODAY = D(2026, 9, 26)
NAMES = {m.key: m.name for m in labs.CATALOGUE}


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    FakeClient.instances.clear()
    FakeClient.scripts.clear()


def _r(d: D, key: str, value: float, low: float | None = None, high: float | None = None,
       flag: str | None = None, unit: str = "u") -> dict[str, Any]:
    row = {"test_date": d, "marker_key": key, "marker_name": key, "value_num": value, "value_text": f"{value:g}",
           "qualifier": None, "unit": unit, "ref_low": low, "ref_high": high, "flag": flag}
    row["status"] = labs.out_of_range(row)
    return row


def _panel(d: D, **overrides: float) -> list[dict[str, Any]]:
    return [_r(d, k, overrides.get(k, 10), 1, 100) for k in PANEL]


def _ctx(results: list[dict[str, Any]], **kw: Any) -> Context:
    return Context(today=TODAY, results=results, names=NAMES, t=dict(DEFAULTS), **kw)


def _by_key(specs: list[Any]) -> dict[str, Any]:
    return {s.rule_key: s for s in specs}


# ---------------------------------------------------------------------------
# Catalogue and parsing
# ---------------------------------------------------------------------------


def test_catalogue_maps_polish_names() -> None:
    assert labs.marker_key("Hemoglobina") == "hemoglobin"
    assert labs.marker_key("Witamina D3 metabolit 25(OH)") == "vitamin_d_25oh"
    assert labs.marker_key("Cholesterol HDL") == "hdl" and labs.marker_key("Cholesterol") == "cholesterol_total"
    assert labs.marker_key("Bazofile %") is None
    assert labs.parse_value("<8.00") == (8.0, "<") and labs.parse_range("13.5 - 18") == (13.5, 18.0)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def test_panel_due_six_months_after_last_full_panel() -> None:
    results = _panel(D(2026, 5, 7))
    assert last_full_panel(results) == D(2026, 5, 7)
    specs = _by_key(evaluate(_ctx(results))[0])
    assert specs["panel:2026-05-07"].due_date == D(2026, 11, 5)


def test_season_panels_merge_when_close() -> None:
    results = _panel(D(2026, 5, 7))
    specs = _by_key(evaluate(_ctx(results, build_start=D(2026, 12, 1)))[0])
    # Pre-build panel (3 Nov) falls within 45 days of the regular one (5 Nov): one check.
    assert "panel:2026-05-07" not in specs and specs["panel_build:2026-12-01"].due_date == D(2026, 11, 3)
    assert "Regular athlete panel" in specs["panel_build:2026-12-01"].rationale


def test_flagged_and_near_limit_follow_ups() -> None:
    results = [*_panel(D(2025, 5, 13), tsh=3.0), *_panel(D(2026, 5, 7), tsh=4.1)]
    results = [r for r in results if r["marker_key"] not in ("tsh", "hemoglobin")]
    results += [_r(D(2025, 5, 13), "tsh", 3.0, 0.27, 4.2), _r(D(2026, 5, 7), "tsh", 4.1, 0.27, 4.2),
                _r(D(2026, 5, 7), "hemoglobin", 18.6, 13.5, 18)]
    spec = _by_key(evaluate(_ctx(results))[0])["followup:2026-05-07"]
    assert set(spec.markers) == {"tsh", "hemoglobin"}
    assert spec.due_date == D(2026, 7, 2)  # flagged: +8 weeks wins over near-limit +12 weeks
    assert "near the top of the range" in spec.rationale and "+37%" in spec.rationale
    assert "above the lab range" in spec.rationale


def test_old_flagged_result_goes_into_next_panel() -> None:
    results = [*_panel(D(2026, 5, 7)), _r(D(2024, 7, 5), "ldh", 250, 135, 225)]
    specs = _by_key(evaluate(_ctx(results))[0])
    assert not any(k.startswith("followup:") for k in specs)
    assert "ldh" in specs["panel:2026-05-07"].markers and "Not re-tested since" in specs["panel:2026-05-07"].rationale


def test_planned_recheck_from_note_until_result() -> None:
    note = {"id": 7, "title": "Blood results May 2026", "category": "health", "body": "",
            "valid_from": D(2026, 5, 7),
            "rules": {"planned_labs": [{"markers": ["tsh"], "due": "2026-06-15", "after": "2026-05-07"}]}}
    results = [*_panel(D(2026, 5, 7)), _r(D(2025, 5, 13), "tsh", 3.0, 0.27, 4.2), _r(D(2026, 5, 7), "tsh", 4.1, 0.27, 4.2)]
    specs = _by_key(evaluate(_ctx(results, notes=[note]))[0])
    assert specs["planned:7:0"].due_date == D(2026, 6, 15)
    assert "tsh" not in (specs.get("followup:2026-05-07").markers if "followup:2026-05-07" in specs else [])
    results.append(_r(D(2026, 7, 1), "tsh", 2.5, 0.27, 4.2))
    assert "planned:7:0" not in _by_key(evaluate(_ctx(results, notes=[note]))[0])


def test_stale_vitamin_d_timed_for_late_winter() -> None:
    results = [r for r in _panel(D(2026, 5, 7)) if r["marker_key"] != "vitamin_d_25oh"]
    results.append(_r(D(2025, 5, 13), "vitamin_d_25oh", 30))
    spec = _by_key(evaluate(_ctx(results))[0])["stale:vitamin_d_25oh:2027"]
    assert spec.due_date == D(2027, 2, 15)


def test_pre_race_exam_and_physio() -> None:
    notes = [
        {"id": 3, "category": "injury", "title": "Shoulder", "body": "treated by a physio", "valid_from": None,
         "created": D(2026, 9, 20), "rules": None},
        {"id": 4, "category": "constraint", "title": "Illness rules", "body": "flagged as a cardiac risk",
         "valid_from": None, "created": D(2026, 9, 20), "rules": None},
    ]
    race = {"name": "Example Ironman", "race_date": D(2027, 8, 22), "distance": "full"}
    specs = _by_key(evaluate(_ctx(_panel(D(2026, 5, 7)), a_race=race, build_start=D(2027, 4, 26), notes=notes))[0])
    exam = specs["prerace_exam:2027-08-22"]
    assert exam.due_date == D(2027, 4, 26) and "Illness rules" in exam.rationale
    assert specs["physio:3:2026-09-20"].due_date == D(2026, 11, 1)
    later = _by_key(evaluate(_ctx(_panel(D(2026, 5, 7)), notes=notes, physio_done={3: D(2026, 10, 30)}))[0])
    assert later["physio:3:2026-10-30"].due_date == D(2026, 12, 11)


def test_trend_watch() -> None:
    results = [_r(D(2024, 5, 1), "hemoglobin", 18.0, 13.5, 18), _r(D(2025, 5, 1), "hemoglobin", 17.2, 13.5, 18),
               _r(D(2026, 5, 1), "hemoglobin", 16.1, 13.5, 18)]
    trend = evaluate(_ctx(results))[1]
    assert trend[0]["key"] == "hemoglobin" and trend[0]["direction"] == "falling"


# ---------------------------------------------------------------------------
# Stored checks
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "h.duckdb").connect()
    yield database
    database.close()


def _store(db: Database, d: D, markers: list[tuple[str, str, str | None]]) -> None:
    labs.import_panel(db, labs.LabPanelIn(test_date=d, markers=[
        labs.LabMarkerIn(name=n, value=v, reference_range=r) for n, v, r in markers]), "manual")


def test_sync_creates_resolves_and_respects_dismissal(db: Database) -> None:
    _store(db, D(2026, 5, 7), [("Ferrytyna", "20", "30 - 400"), ("Hemoglobina", "15", "13.5 - 18")])
    health.sync_checks(db, TODAY)
    follow = next(c for c in health.checks(db) if c["rule_key"] == "followup:2026-05-07")
    assert follow["status"] == "open" and follow["markers"] == ["ferritin"]
    # A new result for ferritin resolves the follow-up with that lab date.
    _store(db, D(2026, 9, 1), [("Ferrytyna", "60", "30 - 400")])
    health.sync_checks(db, TODAY)
    follow = next(c for c in health.checks(db) if c["id"] == follow["id"])
    assert follow["status"] == "done" and follow["lab_date"] == D(2026, 9, 1)
    # Dismissed checks aren't recreated for the same key.
    stale = next(c for c in health.checks(db) if c["rule_key"] == "panel:none")
    health.dismiss(db, stale["id"])
    health.sync_checks(db, TODAY)
    assert [c["status"] for c in health.checks(db) if c["rule_key"] == stale["rule_key"]] == ["dismissed"]


def test_snooze_and_recurring_manual(db: Database) -> None:
    check_id = health.create_check(db, health.CheckIn(kind="other", title="Dentist", due_date=TODAY, interval_days=182))
    health.snooze(db, check_id, 2, TODAY)
    health.sync_checks(db, TODAY + datetime.timedelta(days=13))
    assert health.checks(db, "snoozed")[0]["id"] == check_id
    health.sync_checks(db, TODAY + datetime.timedelta(days=14))
    assert any(c["id"] == check_id for c in health.checks(db, "open"))
    out = health.mark_done(db, check_id, TODAY)
    nxt = next(c for c in health.checks(db) if c["id"] == out["next_id"])
    assert nxt["due_date"] == TODAY + datetime.timedelta(days=182) and nxt["status"] == "open"


def test_unknown_markers_rejected(db: Database) -> None:
    with pytest.raises(health.HealthError):
        health.create_check(db, health.CheckIn(title="x", markers=["unobtainium"]))


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from hart.server.app import create_app

    db_path = tmp_path / "app.duckdb"
    seed_dir = tmp_path / "data" / "seed"
    seed_dir.mkdir(parents=True)
    (tmp_path / "data" / "blood_results.json").write_text(json.dumps([
        {"date": "2025-05-13", "markers": [
            {"name": "TSH", "value": "3.1", "unit": "µIU/ml", "reference_range": "0.27 - 4.2"},
            {"name": "LDH", "value": "250", "unit": "U/l", "reference_range": "135 - 225", "flag": "H"}]},
    ]), encoding="utf-8")
    config = dataclasses.replace(get_config(), db_path=db_path, server=ServerSettings(env="production", seed_dir=seed_dir))
    app = create_app(config, run_scheduler=False, claude_client_factory=FakeClient)
    with TestClient(app, base_url="https://hart.example.ts.net") as c:
        yield c


def test_health_page_and_alerts(client) -> None:
    text = client.get("/health", headers=H).text
    assert "Vital Signs" in text and "Athlete blood panel" in text and "TSH" in text
    # The stale TSH / LDH follow-up are due, so the dashboard warns.
    assert "health check" in client.get("/", headers=H).text
    imported = [r for r in client.get("/api/health/labs", headers=H).json() if r["marker_name"] == "LDH"][0]
    assert client.request("DELETE", f"/api/health/labs/{imported['id']}", headers=W, json={}).status_code == 400


def test_lab_paste_import(client) -> None:
    FakeClient.scripts.append([_result(structured_output={
        "panels": [{"test_date": "2026-09-20", "markers": [
            {"name": "Ferrytyna", "value": "95", "unit": "ng/ml", "reference_range": "30 - 400"},
            {"name": "TSH", "value": "2.1", "unit": "µIU/ml", "reference_range": "0.27 - 4.2"}]}],
        "warnings": [],
    })])
    out = client.post("/api/health/labs/parse", headers=W, json={"text": "Ferrytyna 95 ng/ml 30 - 400\nTSH 2.1"}).json()
    assert out["panels"][0]["markers"][0]["marker_key"] == "ferritin" and out["panels"][0]["existing"] == 0
    assert FakeClient.instances[0].options.model == "claude-haiku-4-5"
    body = {"panels": [{"test_date": p["test_date"], "markers": [
        {k: m[k] for k in ("name", "value", "unit", "reference_range", "flag")} for m in p["markers"]]} for p in out["panels"]]}
    saved = client.post("/api/health/labs/import", headers=W, json=body).json()
    assert saved["panels"][0]["added"] == 2
    again = client.post("/api/health/labs/import", headers=W, json=body).json()
    assert again["panels"][0]["skipped"] == 2
    new = [r for r in client.get("/api/health/labs?marker=tsh", headers=H).json() if str(r["test_date"]) == "2026-09-20"]
    assert new and new[0]["source"] == "paste"


def test_claude_proposed_check_needs_approval(client) -> None:
    from hart import mcp_server

    out = json.loads(mcp_server.propose_health_check(
        title="Re-test ferritin", rationale="Ferritin trending down", kind="follow_up", markers=["ferritin"]))
    assert out["status"] == "proposed"
    assert "Suggested by Ember" in client.get("/health", headers=H).text
    client.post(f"/api/health/checks/{out['id']}/approve", headers=W, json={})
    listed = json.loads(mcp_server.get_health_checks())["checks"]
    assert next(c for c in listed if c["id"] == out["id"])["status"] == "open"
    bad = json.loads(mcp_server.propose_health_check(title="x", rationale="y", markers=["nope"]))
    assert "unknown marker" in bad["error"]
