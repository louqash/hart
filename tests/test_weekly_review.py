"""Weekly review: facts, phase re-plans, schedule, the Ember run, Discord and the Season page."""

from __future__ import annotations

import dataclasses
import datetime
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from claude_agent_sdk import AssistantMessage, SystemMessage, ToolResultBlock, ToolUseBlock, UserMessage

from hart.analytics import weekly_review as wr
from hart.analytics.plan_compare import compare_to_plan
from hart.config import ServerSettings, get_config
from hart.server import season_ops, settings, weekly_review
from hart.server.claude.policy import MCP_PREFIX
from hart.storage.database import Database
from tests.test_chat import FakeClient, _result
from tests.test_grading import H, W, _activity, _wait_job

D = datetime.date
DAY = datetime.timedelta(days=1)
TZ = ZoneInfo("Europe/Warsaw")


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    FakeClient.instances.clear()
    FakeClient.scripts.clear()


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "w.duckdb").connect()
    yield database
    database.close()


# ---------------------------------------------------------------------------
# Facts (analytics)
# ---------------------------------------------------------------------------


def test_review_week() -> None:
    assert wr.review_week(D(2026, 10, 12)) == (D(2026, 10, 5), D(2026, 10, 11))  # Monday: the week that ended
    assert wr.review_week(D(2026, 10, 14)) == (D(2026, 10, 12), D(2026, 10, 18))  # Wednesday: this week so far
    assert wr.review_week(D(2026, 10, 18)) == (D(2026, 10, 12), D(2026, 10, 18))  # Sunday: this week
    assert wr.days_left(D(2026, 10, 18), D(2026, 10, 19)) == 0
    assert wr.days_left(D(2026, 10, 18), D(2026, 10, 18)) == 1
    assert wr.season_weeks(D(2026, 10, 12), D(2026, 9, 17), 6, 26) == 6  # at least the trend
    assert wr.season_weeks(D(2026, 10, 12), D(2026, 7, 1), 6, 26) == 16
    assert wr.season_weeks(D(2026, 10, 12), D(2025, 1, 1), 6, 26) == 26


def _plan(d: D, sport: str, minutes: int, activity_id: str | None = None, source: str = "coach_import") -> dict:
    return {"date": d, "sport_type": sport, "title": f"{sport} {minutes}", "duration_min": minutes,
            "intensity": "endurance", "source": source, "activity_id": activity_id}  # fmt: skip


def test_plan_adherence() -> None:
    mon = D(2026, 10, 12)
    planned = [
        _plan(mon, "run", 40, "r1"),
        _plan(mon + DAY, "bike", 60),  # Tuesday, not done
        _plan(mon + 2 * DAY, "run", 30),  # Wednesday = today
        _plan(mon + 4 * DAY, "run", 50),  # Friday
        _plan(mon + 5 * DAY, "rest", 0),
    ]
    sessions = [
        {"activity_id": "r1", "date": mon, "sport_type": "run", "minutes": 44},
        {"activity_id": "s1", "date": mon + DAY, "sport_type": "strength", "minutes": 45},
        {"activity_id": "w1", "date": mon + DAY, "sport_type": "other", "minutes": 90},  # a walk
    ]
    out = wr.plan_adherence(planned, sessions, mon + 2 * DAY)
    assert [s["status"] for s in out["sessions"]] == ["done", "missed", "today", "upcoming"]
    assert out["sessions"][0]["done_vs_planned_pct"] == 110 and out["sessions"][0]["from"] == "coach"
    assert out["unplanned"] == [{"date": mon + DAY, "sport": "strength", "minutes": 45, "activity_id": "s1"}]
    run = out["by_sport"]["run"]
    assert (run["planned"], run["planned_due"], run["done_as_planned"], run["missed"]) == (3, 1, 1, 0)
    assert out["by_sport"]["bike"]["missed"] == 1 and "other" not in out["by_sport"]
    assert out["totals"] == {
        "planned": 4,
        "planned_due": 2,
        "done_as_planned": 1,
        "done_off_plan": 0,
        "missed": 1,
        "unplanned": 1,
        "completion_pct": 50,
    }
    assert "vs_plan" not in out["sessions"][0]  # no comparison: the link is all there is


def test_a_swapped_session_is_done_but_not_as_planned() -> None:
    mon = D(2026, 10, 12)
    planned = [
        {**_plan(mon, "run", 40, "r1"), "id": 1},
        {**_plan(mon + DAY, "bike", 90, "b1"), "id": 2, "title": "Threshold 3x12", "intensity": "threshold"},
    ]
    sessions = [
        {"activity_id": "r1", "date": mon, "sport_type": "run", "minutes": 42},
        {"activity_id": "b1", "date": mon + DAY, "sport_type": "bike", "minutes": 30},  # an easy spin instead
    ]
    comparisons = {
        1: compare_to_plan(planned_sport="run", planned_minutes=40, planned_intensity="endurance", done_sport="run",
                           done_seconds=42 * 60, hr_zone_seconds={"Z1": 600, "Z2": 1800, "Z3": 120}),
        2: compare_to_plan(planned_sport="bike", planned_minutes=90, planned_intensity="threshold", done_sport="bike",
                           done_seconds=30 * 60, hr_zone_seconds={"Z1": 900, "Z2": 900}),
    }  # fmt: skip
    out = wr.plan_adherence(planned, sessions, mon + 2 * DAY, comparisons)

    run, bike = out["sessions"]
    assert run["status"] == bike["status"] == "done"
    assert run["vs_plan"]["verdict"] == "as_planned" and run["vs_plan"]["deviations"] == []
    assert bike["vs_plan"] == {
        "verdict": "easier",
        "deviations": ["easier", "shorter"],
        "summary": comparisons[2]["summary"],
    }
    assert bike["vs_plan"]["summary"].startswith("Planned bike 90 min threshold; did bike 30 min")
    b = out["by_sport"]["bike"]
    assert (b["planned_due"], b["done_as_planned"], b["done_off_plan"], b["missed"], b["sessions"]) == (1, 0, 1, 0, 1)
    assert out["totals"] == {
        "planned": 2,
        "planned_due": 2,
        "done_as_planned": 1,
        "done_off_plan": 1,
        "missed": 0,
        "unplanned": 0,  # the spin is linked to the plan, not a session without one
        "completion_pct": 50,
    }

    card = wr.scorecard(out, [], {"graded": 0})
    assert card["done_as_planned"] == 1 and card["completion_pct"] == 50
    assert "🚴 Bike · 0/1 planned · 1 done" in weekly_review.scorecard_lines(card)
    assert wr.off_plan(comparisons[2]) and not wr.off_plan(comparisons[1]) and not wr.off_plan(None)


def test_target_status_tells_a_pattern_from_a_one_off() -> None:
    weeks = [{"run": 3}, {"run": 1}, {"run": 2}, {"run": 1, "strength": 2}]
    run, strength = wr.target_status({"run": 3, "strength": 2}, weeks)
    assert (run["done"], run["short_by"], run["met"], run["short_streak_weeks"]) == (1, 2, False, 3)
    assert run["short_in_recent_weeks"] == "3 of 4" and run["previous_weeks"] == [3, 1, 2]
    assert strength["met"] and strength["short_streak_weeks"] == 0
    one_off = wr.target_status({"run": 3}, [{"run": 3}, {"run": 3}, {"run": 2}], left=2)[0]
    assert one_off["short_streak_weeks"] == 1 and one_off["still_possible"]


def test_volume_change_and_grades() -> None:
    weeks = [
        {"minutes": 0, "load": 0},  # a week off isn't part of the baseline
        {"minutes": 300, "load": 200, "by_sport": {"bike": {"minutes": 200}, "run": {"minutes": 100}}},
        {"minutes": 360, "load": 220, "by_sport": {"bike": {"minutes": 240}, "run": {"minutes": 120}}},
        {"minutes": 240, "load": 150, "by_sport": {"bike": {"minutes": 240}}},
    ]
    change = wr.volume_change(weeks)
    assert change["compared_with_weeks"] == 2 and change["minutes"]["average_before"] == 330
    assert change["minutes_change_pct"] == -27 and change["minutes_by_sport"]["run"]["this_week"] == 0
    assert wr.volume_change(weeks[:1]) is None
    grades = wr.grade_summary(
        [
            {"activity_id": "a", "overall_score": 4.2, "letter": "B", "confidence": "high"},
            {"activity_id": "b", "overall_score": 2.4, "letter": "D", "confidence": "low"},
            {"activity_id": "c", "overall_score": None},
        ]
    )
    assert grades["graded"] == 2 and grades["average_score"] == 3.3 and grades["low_confidence"] == 1
    assert grades["lowest"]["activity_id"] == "b" and wr.grade_summary([]) == {"graded": 0}


def test_review_facts_compare_each_linked_session_with_its_plan(db: Database) -> None:
    mon = D(2026, 10, 12)
    seven = datetime.time(7)
    _activity(db, "ride", datetime.datetime.combine(mon, seven), secs=1800)  # Z1–Z3 only, half an hour
    _activity(db, "run", datetime.datetime.combine(mon + DAY, seven), sport="run", sub="road_running", secs=2400,
              power=None)  # fmt: skip
    _activity(db, "old", datetime.datetime.combine(mon - 7 * DAY, seven), secs=5400)
    db.execute(
        "INSERT INTO planned_sessions (date, sport_type, title, duration_min, intensity, source, activity_id) VALUES "
        "(?, 'bike', 'Threshold 3x12', 90, 'threshold', 'coach_import', 'ride'), "
        "(?, 'run', 'Easy run', 40, 'endurance', 'coach_import', 'run'), "
        "(?, 'bike', 'Long ride', 90, 'endurance', 'coach_import', 'old')",
        [mon, mon + DAY, mon - 7 * DAY],
    )
    facts = weekly_review.build_context(db, mon, mon + 7 * DAY)

    adherence = facts["plan_adherence"]
    ride, run = adherence["sessions"]
    assert ride["status"] == "done" and ride["vs_plan"]["verdict"] == "easier"
    assert ride["vs_plan"]["deviations"] == ["easier", "shorter"]
    assert "Planned bike 90 min threshold; did bike 30 min" in ride["vs_plan"]["summary"]
    assert run["vs_plan"]["verdict"] == "as_planned"
    assert adherence["totals"]["done_as_planned"] == 1 and adherence["totals"]["done_off_plan"] == 1
    assert adherence["by_sport"]["bike"]["done_off_plan"] == 1 and adherence["totals"]["completion_pct"] == 50
    assert facts["scorecard"]["done_as_planned"] == 1

    *_, before, this = facts["trend"]["weeks"]
    assert (this["plan_due"], this["plan_done_as_planned"], this["plan_done_off_plan"]) == (2, 1, 1)
    assert this["plan_completion_pct"] == 50
    assert (before["plan_done_as_planned"], before["plan_done_off_plan"]) == (1, 0)


# ---------------------------------------------------------------------------
# Phase re-plans: one proposal moves a boundary
# ---------------------------------------------------------------------------


def _phases(db: Database) -> dict[str, int]:
    ids = {}
    for name, kind, start, end in (
        ("Base 1", "base", D(2026, 10, 5), D(2026, 11, 1)),
        ("Build", "build", D(2026, 11, 2), D(2026, 12, 27)),
        ("Peak", "peak", D(2026, 12, 28), D(2027, 1, 10)),
    ):
        ids[name] = db.fetchone(
            "INSERT INTO training_phases (phase_type, name, start_date, end_date, source, confirmed) "
            "VALUES (?, ?, ?, ?, 'auto', TRUE) RETURNING id",
            [kind, name, start, end],
        )[0]
    return ids


def test_phase_replan_moves_a_boundary(db: Database) -> None:
    ids = _phases(db)
    longer_base = {"end_date": "2026-11-15"}
    with pytest.raises(season_ops.SeasonError, match="Overlaps"):  # one phase at a time can't do it
        season_ops.create_proposal(db, "phase", "update", "longer base", ids["Base 1"], longer_base)
    changes = [{"id": ids["Base 1"], **longer_base}, {"id": ids["Build"], "start_date": "2026-11-16"}]
    prop = season_ops.create_proposal(db, "phases", "update", "Aerobic base not there yet", None, {"changes": changes})
    assert prop["summary"] == (
        "Re-plan phases — “Base 1”: end date 2026-11-01 → 2026-11-15; “Build”: start date 2026-11-02 → 2026-11-16"
    )
    assert db.fetchone("SELECT end_date FROM training_phases WHERE id = ?", [ids["Base 1"]])[0] == D(2026, 11, 1)
    out = season_ops.apply_proposal(db, prop["id"])
    assert out["status"] == "applied"
    rows = db.fetchall("SELECT name, start_date, end_date, source FROM training_phases ORDER BY start_date")
    assert rows[:2] == [
        ("Base 1", D(2026, 10, 5), D(2026, 11, 15), "manual"),
        ("Build", D(2026, 11, 16), D(2026, 12, 27), "manual"),
    ]


def test_phase_replan_validation(db: Database) -> None:
    ids = _phases(db)
    with pytest.raises(season_ops.SeasonError, match="would overlap"):
        season_ops.create_proposal(
            db, "phases", "update", "x", None, {"changes": [{"id": ids["Base 1"], "end_date": "2026-11-15"}]}
        )
    with pytest.raises(season_ops.SeasonError, match="needs"):
        season_ops.create_proposal(db, "phases", "update", "x", None, {"changes": [{"name": "Extra"}]})
    # Delete a phase and add another in its place, together.
    changes = [
        {"id": ids["Peak"], "delete": True},
        {"name": "Recovery", "phase_type": "transition", "start_date": "2026-12-28", "end_date": "2027-01-03"},
    ]
    prop = season_ops.create_proposal(db, "phases", "update", "x", None, {"changes": changes})
    # The calendar changed before it was applied: checked again, refused.
    db.execute("UPDATE training_phases SET end_date = '2027-01-05' WHERE id = ?", [ids["Build"]])
    with pytest.raises(season_ops.SeasonError, match="no longer fits"):
        season_ops.apply_proposal(db, prop["id"])
    db.execute("UPDATE training_phases SET end_date = '2026-12-27' WHERE id = ?", [ids["Build"]])
    season_ops.apply_proposal(db, prop["id"])
    assert [r[0] for r in db.fetchall("SELECT name FROM training_phases ORDER BY start_date")] == [
        "Base 1",
        "Build",
        "Recovery",
    ]


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def _sync_at(db: Database, when: datetime.datetime) -> None:
    db.execute("INSERT INTO jobs (type, status, trigger, finished_at) VALUES ('sync', 'ok', 'schedule', ?)", [when])


def test_due_waits_for_a_sync_after_the_scheduled_time(db: Database) -> None:
    mon = datetime.datetime(2026, 10, 12, tzinfo=TZ)
    _activity(db, "ride", datetime.datetime(2026, 10, 10, 9))
    # Before 06:00 the latest slot is last Monday's: that week is still due until it's reviewed.
    assert weekly_review.due(db, mon.replace(hour=5, minute=59)) == D(2026, 9, 28)
    weekly_review.store(db, D(2026, 9, 28), {"made_on": D(2026, 10, 5), "status": "ok"})
    assert weekly_review.due(db, mon.replace(hour=5, minute=59)) is None
    _sync_at(db, datetime.datetime(2026, 10, 11, 23, tzinfo=TZ))  # Sunday night's sync is too early
    assert weekly_review.due(db, mon.replace(hour=6, minute=30)) is None
    assert weekly_review.due(db, mon.replace(hour=9, minute=1)) == D(2026, 10, 5)  # runs anyway by 09:00
    _sync_at(db, mon.replace(hour=6, minute=4))
    assert weekly_review.due(db, mon.replace(hour=6, minute=30)) == D(2026, 10, 5)
    assert weekly_review.due(db, datetime.datetime(2026, 10, 14, 12, tzinfo=TZ)) == D(2026, 10, 5)  # catches up

    # A review of the week made on Sunday (the week so far) doesn't count; one made on Monday does.
    weekly_review.store(db, D(2026, 10, 5), {"made_on": D(2026, 10, 11), "status": "ok"})
    assert weekly_review.due(db, mon.replace(hour=7)) == D(2026, 10, 5)
    weekly_review.store(db, D(2026, 10, 5), {"made_on": D(2026, 10, 12), "status": "ok"})
    assert weekly_review.due(db, mon.replace(hour=7)) is None


def test_due_off_failures_and_empty_database(db: Database) -> None:
    mon = datetime.datetime(2026, 10, 12, 10, tzinfo=TZ)
    assert weekly_review.due(db, mon) is None  # no training at all: nothing to review
    _activity(db, "ride", datetime.datetime(2026, 10, 10, 9))
    assert weekly_review.due(db, mon) == D(2026, 10, 5)
    for _ in range(weekly_review.MAX_FAILURES):
        weekly_review.store_failure(db, D(2026, 10, 5), D(2026, 10, 12), "schedule", "boom")
    assert weekly_review.due(db, mon) is None
    settings.set_value(db, "weekly_review_day", "sun")
    sun = datetime.datetime(2026, 10, 18, 7, tzinfo=TZ)
    assert weekly_review.due(db, sun) is None  # waits for a sync from about 06:00 on
    assert weekly_review.due(db, sun.replace(hour=10)) == D(2026, 10, 12)  # Sunday: this week so far
    settings.set_value(db, "weekly_review_day", "off")
    assert weekly_review.due(db, sun.replace(hour=10)) is None


def test_scheduler_enqueues_the_review(db: Database) -> None:
    from hart.server.jobs.scheduler import Scheduler
    from tests.test_server import RecordingRunner

    _activity(db, "ride", datetime.datetime(2026, 10, 10, 9))
    _sync_at(db, datetime.datetime(2026, 10, 12, 6, 2, tzinfo=TZ))
    runner = RecordingRunner()
    Scheduler(db, runner, "Europe/Warsaw").tick(datetime.datetime(2026, 10, 12, 6, 5, tzinfo=TZ))  # type: ignore[arg-type]
    assert "weekly_review" in runner.calls


# ---------------------------------------------------------------------------
# The run, through the app
# ---------------------------------------------------------------------------

TODAY = datetime.date.today()
WEEK, WEEK_END = wr.review_week(TODAY)

REVIEW = {
    "analysis": "Runs short three weeks running; the bike sessions were done as planned.",
    "verdict": "behind",
    "headline": "The rides happened, the runs didn't.",
    "summary": "One of three runs. Bike work as planned.",
    "went_well": ["Every planned ride was done"],
    "missing": ["2 of 3 runs"],
    "season": "Base stays — proposed nothing.",
    "next_week": ["Put the runs first"],
    "confidence": "high",
    "citations": [
        {"label": "runs done", "value": 1, "source": "facts.targets"},
        {"label": "run target", "value": 3, "source": "facts.targets"},
    ],
}


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient

    from hart.server.app import create_app

    db_path = tmp_path / "app.duckdb"
    seed = Database(db_path).connect()
    start = datetime.datetime.combine(WEEK, datetime.time(7))
    _activity(seed, "ride", start)
    _activity(seed, "run", start + DAY, sport="run", sub="road_running", secs=2400, power=None)
    _activity(seed, "old", start - 14 * DAY)
    seed.execute(
        "INSERT INTO planned_sessions (date, sport_type, title, duration_min, source, activity_id) VALUES "
        "(?, 'bike', 'Endurance ride', 60, 'coach_import', 'ride')",
        [WEEK],
    )
    seed.execute(
        "INSERT INTO athlete_notes (category, title, body, rules, status, source) VALUES "
        "('constraint', 'Weekly structure', 'Three runs a week', ?, 'active', 'manual')",
        [json.dumps({"min_sessions_per_week": {"run": 3}})],
    )
    seed.close()
    base = get_config()
    config = dataclasses.replace(
        base,
        db_path=db_path,
        server=ServerSettings(env="production", seed_dir=tmp_path / "seed", public_host="hart.example.ts.net"),
        discord=dataclasses.replace(base.discord, webhook_url="https://discord.example/webhook", bot_token=""),
    )
    sent: list[dict[str, Any]] = []

    class Response:
        status_code = 204
        text = ""

    import httpx

    monkeypatch.setattr(httpx, "post", lambda url, **kw: sent.append(kw["json"]) or Response())
    app = create_app(config, run_scheduler=False, claude_client_factory=FakeClient)
    with TestClient(app, base_url="https://hart.example.ts.net") as c:
        c.sent = sent  # type: ignore[attr-defined]
        yield c


def _run(client, **body: Any) -> dict[str, Any]:
    job = client.post("/api/weekly-reviews/run", headers=W, json=body).json()
    return _wait_job(client, job["job_id"])


def _script(review: dict[str, Any], proposal: dict[str, Any] | None = None) -> list[Any]:
    out: list[Any] = [SystemMessage(subtype="init", data={"session_id": "s1"})]
    if proposal is not None:
        out += [
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name=MCP_PREFIX + "propose_season_change", input={"reason": "why"})],
                model="claude-opus-5-5",
            ),
            UserMessage(
                content=[ToolResultBlock(tool_use_id="t1", content=[{"type": "text", "text": json.dumps(proposal)}])]
            ),
        ]
    return [*out, _result(structured_output=review)]


def test_first_review_goes_over_the_season_and_is_posted(client) -> None:
    db = client.app.state.db
    phase = db.fetchone(
        "INSERT INTO training_phases (phase_type, name, start_date, end_date, source, confirmed) "
        "VALUES ('base', 'Base 1', ?, ?, 'auto', TRUE) RETURNING id",
        [WEEK - 21 * DAY, WEEK + 13 * DAY],
    )[0]
    prop = season_ops.create_proposal(db, "phase", "update", "why", phase, {"end_date": str(WEEK + 20 * DAY)})
    FakeClient.scripts.append(_script(REVIEW, {"id": prop["id"], "status": "pending", "summary": prop["summary"]}))
    job = _run(client)
    assert job["status"] == "ok" and job["result"]["verdict"] == "behind" and job["result"]["discord"] == "sent"

    fake = FakeClient.instances[0]
    assert fake.options.model == "claude-opus-5-5" and fake.options.effort == "xhigh"
    assert MCP_PREFIX + "propose_season_change" in fake.options.allowed_tools
    assert MCP_PREFIX + "propose_plan_change" in fake.options.disallowed_tools
    assert "and the whole season since" in fake.prompt  # the first review covers everything

    review = client.get("/api/weekly-reviews", headers=H).json()["reviews"][0]
    assert review["scope"] == "season" and review["confidence"] == "high"
    assert review["proposals"] == [
        {"type": "season", "id": prop["id"], "summary": prop["summary"], "reason": "why", "status": "pending"}
    ]
    full = client.get(f"/api/weekly-reviews/{review['id']}", headers=H).json()
    facts = full["context"]
    assert facts["season_so_far"]["weeks"] and facts["week"]["start"] == str(WEEK)
    run_target = next(t for t in facts["targets"] if t["sport"] == "run")
    assert run_target["done"] == 1 and run_target["target_per_week"] == 3
    assert facts["plan_adherence"]["totals"]["done_as_planned"] == 1

    embed = client.sent[0]["embeds"][0]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert embed["title"].startswith("🧭 Season review · week of") and embed["url"].endswith("/season#weekly-review")
    assert "**⚠️ Behind** — The rides happened" in embed["description"]
    assert "🏃 Run · 1 done · target 3 ✗" in fields["📊 Plan vs done"]
    assert "🚴 Bike · 1/1 planned · target" not in fields["📊 Plan vs done"]  # no bike target
    assert fields["🕳️ Missing"] == "• 2 of 3 runs" and prop["summary"] in fields["📝 Waiting for you"]

    page = client.get("/season", headers=H).text
    assert "Trail Journal" in page and "The rides happened, the runs didn&#39;t." in page and "whole season" in page

    # The next one reviews the week only.
    FakeClient.scripts.append(_script(REVIEW))
    _run(client)
    assert "whole season" not in FakeClient.instances[1].prompt
    assert client.get("/api/weekly-reviews", headers=H).json()["reviews"][0]["version"] == 2


def test_unverified_numbers_are_sent_back_once(client) -> None:
    made_up = {**REVIEW, "citations": [{"label": "made up", "value": 987.6, "source": "?"}]}
    FakeClient.scripts.extend([_script(made_up), _script(made_up)])
    assert _run(client, scope="week")["status"] == "ok"
    assert "weren't found" in FakeClient.instances[1].prompt and "made up = 987.6" in FakeClient.instances[1].prompt
    assert client.get("/api/weekly-reviews", headers=H).json()["reviews"][0]["confidence"] == "low"


def test_invalid_output_twice_is_stored_as_failed(client) -> None:
    FakeClient.scripts.extend([_script({"verdict": "great"}), _script({"verdict": "great"})])
    job = _run(client, scope="week")
    assert job["status"] == "error" and "failed validation" in FakeClient.instances[1].prompt
    review = client.get("/api/weekly-reviews", headers=H).json()["reviews"][0]
    assert review["status"] == "failed" and client.sent == []
    assert "failed" in client.get("/season", headers=H).text


def test_season_page_with_a_failed_earlier_week(client) -> None:
    # Last week's runs failed; this week's review then succeeded: the failure is an "earlier week" row
    # without a scorecard.
    db = client.app.state.db
    weekly_review.store_failure(db, WEEK - 7 * DAY, TODAY, "schedule", "declined")
    FakeClient.scripts.append(_script(REVIEW))
    _run(client)
    page = client.get("/season", headers=H)
    assert page.status_code == 200
    assert "The rides happened" in page.text and "declined" in page.text


FLAGGED = "API Error: Opus 5.5's safeguards flagged this message"


def _refusal() -> list[Any]:
    return [SystemMessage(subtype="init", data={"session_id": "s1"}), _result(is_error=True, errors=[FLAGGED])]


def test_a_declined_review_is_written_by_the_fallback_model(client) -> None:
    FakeClient.scripts.extend([_refusal(), _script(REVIEW)])
    job = _run(client, scope="week")
    assert job["status"] == "ok"
    first, second = FakeClient.instances
    assert first.options.model == "claude-opus-5-5"
    assert (second.options.model, second.options.effort) == (
        "claude-sonnet-5",
        "high",
    ) and second.options.resume is None
    review = client.get("/api/weekly-reviews", headers=H).json()["reviews"][0]
    assert review["review"]["model"] == "claude-sonnet-5" and review["review"]["declined_by"] == "claude-opus-5-5"
    assert "written by claude-sonnet-5" in client.sent[0]["embeds"][0]["footer"]["text"]
    assert "written by claude-sonnet-5" in client.get("/season", headers=H).text


def test_declined_by_both_models_fails_with_the_reason(client) -> None:
    FakeClient.scripts.extend([_refusal(), _refusal()])
    job = _run(client, scope="week")
    assert job["status"] == "error" and len(FakeClient.instances) == 2  # not repaired: it would be declined again
    review = client.get("/api/weekly-reviews", headers=H).json()["reviews"][0]
    assert review["error"].startswith("claude-opus-5-5 declined the review (safeguards); claude-sonnet-5 then failed")
    assert FLAGGED in review["error"]


def test_review_tools_leave_out_lab_results() -> None:
    from hart.server.claude.policy import review_policy

    policy = review_policy()
    assert MCP_PREFIX + "get_lab_results" not in policy.allowed_tools
    assert MCP_PREFIX + "get_lab_results" in policy.disallowed_tools
    assert MCP_PREFIX + "get_session_grade" in policy.allowed_tools


def test_mcp_tool_lists_reviews(client) -> None:
    from tests.test_season_readiness import _mcp

    assert _mcp(client, "get_weekly_reviews", {})["reviews"] == []
    FakeClient.scripts.append(_script(REVIEW))
    _run(client)
    out = _mcp(client, "get_weekly_reviews", {"limit": 2})["reviews"]
    assert out[0]["headline"] == REVIEW["headline"] and "citations" not in out[0]
