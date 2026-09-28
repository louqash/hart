"""Tests: coach plan, coach plan paste import, matching, guardrails,
suggestions, schedules, PWA."""

from __future__ import annotations

import dataclasses
import datetime
import json
import time
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock

from hart.analytics.guardrails import GuardContext, check, limits
from hart.config import ServerSettings, get_config
from hart.server import plan, suggestions
from hart.storage.database import Database
from tests.test_chat import FakeClient, _result
from tests.test_grading import H, W, _activity, _wait_job

D = datetime.date
THU = D(2026, 10, 1)
TUE = D(2026, 9, 29)


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    FakeClient.instances.clear()
    FakeClient.scripts.clear()


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "p.duckdb").connect()
    yield database
    database.close()


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

SWIM_RULES = ("Shoulder", {"allowed_weekdays": {"swim": ["thu"]}, "max_sessions_per_week": {"swim": 1}})


def _s(sport: str = "bike", minutes: int = 45, intensity: str = "endurance") -> dict[str, Any]:
    return {"sport_type": sport, "title": "x", "duration_min": minutes, "intensity": intensity}


def test_red_readiness_blocks_hard_work() -> None:
    ctx = GuardContext(date=TUE, readiness="red")
    assert check("free_choice", [_s(intensity="endurance")], ctx) == []
    assert any("no threshold" in p for p in check("free_choice", [_s(intensity="threshold")], ctx))
    coach = GuardContext(date=TUE, readiness="red", coach_sessions=[_s(minutes=60, intensity="threshold")])
    assert any("readiness is red" in p for p in check("as_planned", [_s(60, intensity="threshold")], coach))
    assert check("modify", [_s(minutes=40, intensity="endurance")], coach) == []


def test_swim_only_on_thursday_once() -> None:
    tue = GuardContext(date=TUE, readiness="green", rules=[SWIM_RULES])
    assert any("only on thu" in p for p in check("free_choice", [_s("swim")], tue))
    thu = GuardContext(
        date=THU, readiness="green", rules=[SWIM_RULES], coach_sessions=[_s("swim", 80)], week_counts={"swim": 0}
    )
    assert check("as_planned", [_s("swim", 80)], thu) == []
    thu.week_counts = {"swim": 1}  # already swam this week
    assert any("allows 1" in p for p in check("as_planned", [_s("swim", 80)], thu))


def test_coach_plan_limits_modify_and_replace() -> None:
    ctx = GuardContext(date=TUE, readiness="amber", coach_sessions=[_s(minutes=60, intensity="tempo")])
    assert any("more than the coach" in p for p in check("modify", [_s(minutes=75)], ctx))
    assert any("harder than" in p for p in check("modify", [_s(minutes=50, intensity="vo2")], ctx))
    assert any("'replace' is only allowed" in p for p in check("replace", [_s(minutes=30)], ctx))
    assert any("not allowed" in p for p in check("free_choice", [_s()], ctx))
    assert any("must not list" in p for p in check("rest", [_s()], ctx))


def test_comeback_cap() -> None:
    ctx = GuardContext(date=TUE, readiness="green", comeback=True, longest_min_14d={"run": 30})
    assert check("free_choice", [_s("run", 37)], ctx) == []
    assert any("comeback cap of 37" in p for p in check("free_choice", [_s("run", 45)], ctx))
    assert limits(ctx)["comeback_max_duration_min"] == {"run": 37}


# ---------------------------------------------------------------------------
# Plan rows and matching
# ---------------------------------------------------------------------------


def _row(d: D, sport: str = "bike", minutes: int = 60, **kw: Any) -> plan.PlanRowIn:
    return plan.PlanRowIn(
        date=d, sport_type=sport, title=kw.pop("title", f"{sport} {minutes}"), duration_min=minutes, **kw
    )


def test_auto_match_picks_closest_duration(db: Database) -> None:
    day = datetime.datetime(2026, 9, 24, 7)
    _activity(db, "short", day, secs=1800)
    _activity(db, "long", day + datetime.timedelta(hours=3), secs=5400)
    _activity(db, "run", day + datetime.timedelta(hours=5), sport="run", sub="running", secs=1800, power=None)
    long_id = plan.create_row(db, _row(day.date(), minutes=90))
    run_id = plan.create_row(db, _row(day.date(), "run", 30))
    rows = {r["id"]: r for r in plan.plan_rows(db, day.date(), day.date())}
    assert rows[long_id]["activity_id"] == "long" and rows[long_id]["match_method"] == "auto"
    assert rows[run_id]["activity_id"] == "run"
    # A manual "not done" sticks; re-matching leaves it alone.
    plan.link_activity(db, long_id, None)
    plan.auto_match(db, [day.date()])
    assert plan.plan_rows(db, day.date(), day.date())[0]["activity_id"] is None


def test_import_replaces_earlier_import(db: Database) -> None:
    first = plan.import_rows(db, [_row(TUE, title="old")])
    again = plan.import_rows(db, [_row(TUE, title="new"), _row(THU, "swim", 80)])
    assert first["created"] == 1 and again == {**again, "created": 2, "replaced": 1}
    titles = [r["title"] for r in plan.plan_rows(db, TUE, THU)]
    assert titles == ["new", "swim 80"]


# ---------------------------------------------------------------------------
# App: parse, import, Garmin text, suggestions
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from hart.server.app import create_app

    db_path = tmp_path / "app.duckdb"
    seed = Database(db_path).connect()
    now = datetime.datetime.now().replace(microsecond=0)
    for i in range(1, 5):
        _activity(seed, f"old{i}", now - datetime.timedelta(days=3 * i), hr=135)
    seed.close()
    config = dataclasses.replace(
        get_config(), db_path=db_path, server=ServerSettings(env="production", seed_dir=tmp_path / "seed")
    )
    app = create_app(config, run_scheduler=False, claude_client_factory=FakeClient)
    with TestClient(app, base_url="https://hart.example.ts.net") as c:
        yield c


PASTE = "Monday, September 28\nBike 1:00:00\n15'- progresja do 170W, kadencja >80\n3x(5'-200W >80 + 2'-150W)\n"


def test_parse_and_import(client) -> None:
    FakeClient.scripts.append(
        [
            _result(
                structured_output={
                    "sessions": [
                        {
                            "date": "2026-09-28",
                            "sport_type": "bike",
                            "title": "Progression + 3x5'",
                            "duration_min": 60,
                            "intensity": "tempo",
                            "description": PASTE.split("\n", 2)[2].strip(),
                        }
                    ],
                    "warnings": ["Couldn't read the TSS line"],
                }
            )
        ]
    )
    out = client.post("/api/plan/parse", headers=W, json={"text": PASTE}).json()
    assert out["sessions"][0]["duration_min"] == 60 and out["warnings"]
    opts = FakeClient.instances[0].options
    assert opts.model == "claude-haiku-4-5" and opts.allowed_tools == [] and opts.tools == []
    assert "Reference date" in FakeClient.instances[0].prompt
    saved = client.post("/api/plan/import", headers=W, json={"sessions": out["sessions"]}).json()
    assert saved["created"] == 1
    items = client.get("/api/plan?start=2026-09-28&end=2026-09-28", headers=H).json()["items"]
    assert items[0]["source"] == "coach_import" and "progresja" in items[0]["description"]
    assert "Progression + 3x5" in client.get("/plan", headers=H).text


def test_garmin_text_is_cached(client) -> None:
    row = client.post(
        "/api/plan",
        headers=W,
        json={
            "date": "2026-09-28",
            "sport_type": "bike",
            "title": "Intervals",
            "duration_min": 60,
            "description": "10x1'-250W 75-80 + 1'-150W 75-80",
        },
    ).json()
    FakeClient.scripts.append(
        [
            AssistantMessage(
                content=[TextBlock(text="```text\nSet 1 10x\n- 1m 245-255w 75-80rpm\n- 1m 145-155w 75-80rpm\n```")],
                model="claude-sonnet-5",
            ),
            _result(),
        ]
    )
    out = client.post(f"/api/plan/{row['id']}/garmin", headers=W, json={}).json()
    assert out["garmin_text"].startswith("Set 1 10x") and not out["cached"]
    assert "Garmin Workout Formatter" in FakeClient.instances[0].options.system_prompt
    assert client.post(f"/api/plan/{row['id']}/garmin", headers=W, json={}).json()["cached"] is True
    assert len(FakeClient.instances) == 1


def _suggestion(rec: str, sessions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "week_review": "One ride this week so far.",
        "recommendation": rec,
        "summary": "Shorter and easier today.",
        "cautions": [],
        "sessions": [
            {**s, "rationale": "Readiness is unknown.", "structure": "10' easy\n30' Z2\n5' easy"} for s in sessions
        ],
        "citations": [],
    }


def _regenerate(client, d: D) -> dict[str, Any]:
    job = client.post(f"/api/suggestions/{d}/regenerate", headers=W, json={}).json()
    return _wait_job(client, job["job_id"])


def test_suggestion_repaired_then_accepted(client) -> None:
    today = datetime.date.today()
    coach = client.post(
        "/api/plan",
        headers=W,
        json={
            "date": str(today),
            "sport_type": "bike",
            "title": "Coach ride",
            "duration_min": 60,
            "intensity": "endurance",
            "description": "60' Z2",
        },
    ).json()
    FakeClient.scripts.append([_result(structured_output=_suggestion("modify", [_s(minutes=90)]))])
    FakeClient.scripts.append([_result(structured_output=_suggestion("modify", [_s(minutes=45)]))])
    job = _regenerate(client, today)
    assert job["status"] == "ok" and job["result"]["status"] == "ok"
    assert "more than the coach" in FakeClient.instances[1].prompt  # the repair named the violation
    first = FakeClient.instances[0]
    assert "Garmin workout formatter rules" in first.options.system_prompt
    assert '"coach_text": "60\' Z2"' in first.prompt and '"limits"' in first.prompt

    s = client.get(f"/api/suggestions/{today}", headers=H).json()
    assert s["recommendation"] == "modify" and s["sessions"][0]["duration_min"] == 45
    ids = client.post(f"/api/suggestions/{s['id']}/accept", headers=W, json={}).json()["planned_ids"]
    items = {r["id"]: r for r in client.get(f"/api/plan?start={today}&end={today}", headers=H).json()["items"]}
    assert items[ids[0]]["replaces_id"] == coach["id"] and items[coach["id"]]["replaced_by"] == ids[0]
    assert client.post(f"/api/suggestions/{s['id']}/accept", headers=W, json={}).status_code == 400
    assert "Next Steps" in client.get("/", headers=H).text


def test_suggestion_failing_guardrails_twice_is_not_shown(client) -> None:
    today = datetime.date.today()
    bad = _suggestion("free_choice", [_s(intensity="vo2", minutes=200)])
    with client.app.state.db.cursor() as cur:
        cur.execute(
            "INSERT INTO athlete_notes (category, title, body, rules, status, source) VALUES "
            "('constraint', 'Easy only', 'x', '{\"max_intensity\": {\"bike\": \"endurance\"}}', 'active', 'manual')"
        )
    FakeClient.scripts.extend([[_result(structured_output=bad)], [_result(structured_output=bad)]])
    job = _regenerate(client, today)
    assert job["result"]["status"] == "failed" and "Easy only" in job["result"]["error"]
    s = client.get(f"/api/suggestions/{today}", headers=H).json()
    assert s["status"] == "failed"


def test_race_day_has_no_suggestion(client) -> None:
    race_day = datetime.date.today() + datetime.timedelta(days=3)
    client.post(
        "/api/races",
        headers=W,
        json={"name": "Test Tri", "race_date": str(race_day), "distance": "olympic", "priority": "B"},
    )
    r = client.post(f"/api/suggestions/{race_day}/regenerate", headers=W, json={})
    assert r.status_code == 400 and "Race day" in r.json()["error"]["message"]


def test_pwa_files(client) -> None:
    sw = client.get("/sw.js", headers=H)
    assert (
        sw.status_code == 200 and "caches.open" in sw.text and sw.headers["content-type"].startswith("text/javascript")
    )
    m = client.get("/manifest.webmanifest", headers=H).json()
    assert m["display"] == "standalone" and any(i.get("purpose") == "maskable" for i in m["icons"])
    assert 'rel="manifest"' in client.get("/plan", headers=H).text


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


def test_scheduler_suggestion_slots(db: Database) -> None:
    from hart.server.jobs.scheduler import Scheduler
    from tests.test_server import RecordingRunner, TestScheduler

    at = TestScheduler()._at
    runner = RecordingRunner()
    sched = Scheduler(db, runner, "Europe/Warsaw")  # type: ignore[arg-type]
    sched.suggestions_due(at(10, 0).astimezone(sched._tz))
    assert runner.calls == []
    sched.suggestions_due(at(10, 31).astimezone(sched._tz))
    sched.suggestions_due(at(10, 45).astimezone(sched._tz))  # once per day
    assert runner.calls == ["suggest"]
    sched.suggestions_due(at(20, 1).astimezone(sched._tz))
    assert runner.calls == ["suggest", "suggest"]


def test_final_regenerated_once_after_fallback(db: Database) -> None:
    today = D(2026, 9, 26)
    assert suggestions.should_generate_final(db, today)
    suggestions.store(
        db, today, {"kind": "final", "readiness": "unknown", "status": "ok", "context": {"trigger": "fallback"}}
    )
    assert suggestions.should_generate_final(db, today)
    suggestions.store(
        db, today, {"kind": "final", "readiness": "green", "status": "ok", "context": {"trigger": "sync"}}
    )
    assert not suggestions.should_generate_final(db, today)


# ---------------------------------------------------------------------------
# Weekly targets, history, regeneration (suggest@2)
# ---------------------------------------------------------------------------

SUN = D(2026, 9, 27)
TARGET_RULES = ("Weekly structure", {"min_sessions_per_week": {"strength": 2}})


def test_required_session_guardrail() -> None:
    ctx = GuardContext(date=SUN, readiness="green", required_today={"strength": "1 strength session(s) still owed"})
    assert any("must include a strength" in p for p in check("free_choice", [_s("run", 30)], ctx))
    assert check("free_choice", [_s("strength", 50, "strength")], ctx) == []
    assert any("must include" in p for p in check("rest", [], ctx))
    ctx.readiness = "red"
    assert check("rest", [], ctx) == []
    coach = GuardContext(
        date=SUN, readiness="green", coach_sessions=[_s("run", 30)], required_today={"strength": "owed"}
    )
    assert check("as_planned", [_s("run", 30)], coach) == []  # the coach's plan wins


def _note(db: Database, rules: dict[str, Any]) -> None:
    import json as _json

    db.execute(
        "INSERT INTO athlete_notes (category, title, body, rules, status, source) VALUES "
        "('constraint', 'Weekly structure', 'Two strength sessions: A Tue, B Sat.', ?, 'active', 'manual')",
        [_json.dumps(rules)],
    )


def test_week_targets_and_history(db: Database) -> None:
    fri = datetime.datetime(2026, 9, 25, 18)
    _activity(db, "gym", fri, sport="strength", sub="strength_training", power=None)
    db.execute(
        "INSERT INTO strength_sets (activity_id, set_index, set_type, repetitions, weight_kg, exercise_name) "
        "VALUES ('gym', 0, 'active', 5, 60, 'Deadlift')"
    )
    _activity(db, "ride", datetime.datetime(2026, 9, 26, 10))
    _note(db, {"min_sessions_per_week": {"strength": 2}, "preferred_weekdays": {"strength": ["tue", "sat"]}})
    notes = suggestions.active_notes(db, SUN)
    sun = suggestions.week_targets(db, SUN, notes)[0]
    assert sun["done_this_week"] == 1 and sun["still_owed"] == 1 and sun["required_today"]
    assert sun["preferred_days_missed"] == ["tue", "sat"]
    sat = suggestions.week_targets(db, D(2026, 9, 26), notes)[0]
    assert sat["still_owed"] == 1 and not sat["required_today"]  # Sunday is still free
    history = suggestions.training_history(db, SUN)
    gym = next(h for h in history if h["sport"] == "strength")
    assert gym["exercises"][0] == {"exercise": "Deadlift", "sets": 1, "top_kg": 60, "top_reps": 5}
    assert gym["weekday"] == "Fri"


def test_regeneration_sees_previous_version(client) -> None:
    today = datetime.date.today()
    FakeClient.scripts.append([_result(structured_output=_suggestion("free_choice", [_s("run", 30)]))])
    FakeClient.scripts.append(
        [_result(structured_output={**_suggestion("free_choice", [_s("run", 30)]), "changed_from_previous": None})]
    )
    _regenerate(client, today)
    first = FakeClient.instances[0]
    assert first.options.model == "claude-opus-5-5"
    assert '"training_history"' in first.prompt and "previous_suggestion" not in first.prompt
    _regenerate(client, today)
    second = FakeClient.instances[1].prompt
    assert '"previous_suggestion"' in second and '"changed": []' in second
    s = client.get(f"/api/suggestions/{today}", headers=H).json()
    assert s["version"] == 2 and s["context"]["week_review"] == "One ride this week so far."


# ---------------------------------------------------------------------------
# Coach sessions arriving day by day
# ---------------------------------------------------------------------------


def test_plan_change_refreshes_suggestion(client) -> None:
    today = datetime.date.today()
    FakeClient.scripts.append([_result(structured_output=_suggestion("free_choice", [_s("run", 30)]))])
    _regenerate(client, today)
    # Coach sends today's session later: the free-choice suggestion is outdated → regenerated.
    FakeClient.scripts.append([_result(structured_output=_suggestion("as_planned", [_s("bike", 60)]))])
    out = client.post(
        "/api/plan",
        headers=W,
        json={
            "date": str(today),
            "sport_type": "bike",
            "title": "Coach ride",
            "duration_min": 60,
            "description": "60' Z2",
        },
    ).json()
    assert out["suggestions_refreshed"] == [str(today)]
    for _ in range(300):
        s = client.get(f"/api/suggestions/{today}", headers=H).json()
        if s["version"] == 2 and not s["pending"]:
            break
        time.sleep(0.02)
    assert s["recommendation"] == "as_planned" and s["context"]["trigger"] == "plan_changed"
    # Accepting a suggestion isn't a coach-plan change; nor is a day outside today/tomorrow.
    far = client.post(
        "/api/plan",
        headers=W,
        json={
            "date": str(today + datetime.timedelta(days=5)),
            "sport_type": "run",
            "title": "Later",
            "duration_min": 30,
        },
    ).json()
    assert far["suggestions_refreshed"] == []
    same = client.put(
        f"/api/plan/{out['id']}",
        headers=W,
        json={
            "date": str(today),
            "sport_type": "bike",
            "title": "Coach ride",
            "duration_min": 60,
            "description": "60' Z2",
        },
    ).json()
    assert same["suggestions_refreshed"] == []  # nothing actually changed


def test_paste_default_date_reaches_claude(client) -> None:
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    FakeClient.scripts.append([_result(structured_output={"sessions": [], "warnings": []})])
    client.post("/api/plan/parse", headers=W, json={"text": "45' Z2", "default_date": str(tomorrow)})
    assert f"doesn't date belong to {tomorrow.isoformat()}" in FakeClient.instances[0].prompt


def test_ember_proposes_plan_changes(client) -> None:
    from hart import mcp_server

    day = datetime.date.today() + datetime.timedelta(days=4)
    out = json.loads(
        mcp_server.propose_plan_change(
            action="create",
            reason="Two strength sessions this week",
            date=str(day),
            sport_type="strength",
            title="Session B",
            duration_min=50,
            intensity="strength",
        )
    )
    assert out["status"] == "pending" and "Session B" in out["summary"]
    assert client.get(f"/api/plan?start={day}&end={day}", headers=H).json()["items"] == []  # nothing yet
    assert "Suggested by Ember" in client.get("/plan", headers=H).text
    applied = client.post(f"/api/season/proposals/{out['id']}/apply", headers=W, json={}).json()
    row = client.get(f"/api/plan?start={day}&end={day}", headers=H).json()["items"][0]
    assert applied["planned_id"] == row["id"] and row["title"] == "Session B" and row["duration_min"] == 50
    move = json.loads(
        mcp_server.propose_plan_change(
            action="update", reason="Legs need 48 h", target_id=row["id"], date=str(day + datetime.timedelta(days=1))
        )
    )
    assert "date" in move["summary"]
    client.post(f"/api/season/proposals/{move['id']}/apply", headers=W, json={})
    assert client.get(f"/api/plan?start={day}&end={day}", headers=H).json()["items"] == []
    gone = json.loads(mcp_server.propose_plan_change(action="delete", reason="Rest instead", target_id=row["id"]))
    client.post(f"/api/season/proposals/{gone['id']}/apply", headers=W, json={})
    assert client.get("/api/plan", headers=H).json()["items"] == []
    bad = json.loads(
        mcp_server.propose_plan_change(action="create", reason="x", date=str(day), sport_type="yoga", title="Flow")
    )
    assert "Invalid planned session" in bad["error"]


def test_coach_plan_from_chat_goes_through_the_paste_reader(client) -> None:
    """Ember's import_coach_plan: same parser as the paste box, saved only after Apply on the Plan page."""
    day = "2026-09-28"
    client.post(
        "/api/plan/import",
        headers=W,
        json={"sessions": [{"date": day, "sport_type": "run", "title": "Old coach run", "duration_min": 30}]},
    )
    FakeClient.scripts.append(
        [
            _result(
                structured_output={
                    "sessions": [
                        {
                            "date": day,
                            "sport_type": "bike",
                            "title": "Progression + 3x5'",
                            "duration_min": 60,
                            "intensity": "tempo",
                            "description": "15'- progresja do 170W",
                        }
                    ],
                    "warnings": [],
                }
            )
        ]
    )
    from hart import mcp_server

    tool_out = json.loads(mcp_server.import_coach_plan(PASTE, day, "From Discord"))  # waits for the reader
    assert tool_out["status"] == "pending" and tool_out["sessions"][0]["title"] == "Progression + 3x5'"
    done = {"result": tool_out}
    assert "Reference date" in FakeClient.instances[0].prompt and PASTE.strip() in FakeClient.instances[0].prompt
    assert "replaces the coach sessions already on 2026-09-28" in done["result"]["summary"]

    page = client.get("/plan", headers=H).text
    assert "Sessions as they will be saved" in page and "From Discord" in page
    items = client.get(f"/api/plan?start={day}&end={day}", headers=H).json()["items"]
    assert [i["title"] for i in items] == ["Old coach run"]  # nothing saved yet

    applied = client.post(f"/api/season/proposals/{done['result']['id']}/apply", headers=W, json={}).json()
    assert applied["created"] == 1 and applied["replaced"] == 1
    items = client.get(f"/api/plan?start={day}&end={day}", headers=H).json()["items"]
    assert [(i["title"], i["source"]) for i in items] == [("Progression + 3x5'", "coach_import")]
