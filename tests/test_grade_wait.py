"""Waiting for the athlete's note before grading: the hold, syncs, the end of the wait, Discord and the page."""

from __future__ import annotations

import dataclasses
import datetime
import json
from pathlib import Path
from typing import Any

import pytest

from hart.config import get_config
from hart.ingestion.sync_manager import SyncResult
from hart.server import discord_chat, evening, grade_wait, settings, state
from hart.server.jobs.scheduler import Scheduler, ungraded_recent
from hart.storage.database import Database
from tests.test_chat import _result
from tests.test_grading import GRADE, NOW, FakeClient, H, W, _activity, _wait_job, client  # noqa: F401 — fixture
from tests.test_server import FakeSyncManager

ME, STRANGER = 42, 99
UTC = datetime.UTC
MIN = datetime.timedelta(minutes=1)


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "w.duckdb").connect()
    yield database
    database.close()


@pytest.fixture
def config():
    base = get_config()
    return dataclasses.replace(
        base,
        garmin=dataclasses.replace(base.garmin, email="x@example.com"),
        discord=dataclasses.replace(base.discord, bot_token="t", channel_id=111, allowed_user_ids=(ME,)),
        server=dataclasses.replace(base.server, public_host="hart.example.ts.net"),
    )


@pytest.fixture
def discord(monkeypatch) -> dict[str, list[Any]]:
    """Messages posted and edited, instead of Discord."""
    calls: dict[str, list[Any]] = {"sent": [], "edited": []}

    def send(cfg, message, channel_id=None):
        calls["sent"].append(message)
        return {"id": f"m{len(calls['sent'])}", "channel_id": "111"}

    monkeypatch.setattr(evening, "send", send)
    monkeypatch.setattr(
        evening, "edit", lambda cfg, channel, message_id, message: calls["edited"].append((message_id, message))
    )
    return calls


class Runner:
    def __init__(self, sync_status: str = "queued") -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sync_status = sync_status

    def enqueue(self, job_type: str, payload: Any = None, **kw: Any) -> dict[str, Any]:
        self.calls.append((job_type, payload or {}))
        return {"job_id": len(self.calls), "status": self.sync_status if job_type == "sync" else "queued"}

    def grades(self) -> list[tuple[str, str]]:
        return [(p["activity_id"], p["trigger"]) for t, p in self.calls if t == "grade"]


class Garmin(FakeSyncManager):
    """Each sync brings the activities in ``new`` (id → columns to set, e.g. a description)."""

    health_error = None
    new: dict[str, dict[str, Any]] = {}

    def sync_garmin_activities(self, days_back: int = 7) -> SyncResult:
        for act_id, columns in self.new.items():
            sport = columns.pop("sport_type", "bike")
            _activity(
                self.db, act_id, NOW - datetime.timedelta(hours=1), sport=sport, power=None if sport != "bike" else 150
            )
            for column, value in columns.items():
                self.db.execute(f"UPDATE activities SET {column} = ? WHERE activity_id = ?", [value, act_id])
        count, Garmin.new = len(self.new), {}
        return SyncResult(new_activities=count)


def _sync(db: Database, config, runner: Runner, monkeypatch) -> dict[str, Any]:
    import hart.server.jobs.handlers as handlers
    from hart.server.jobs.pipeline import run_sync_pipeline as original

    monkeypatch.setattr(
        handlers, "run_sync_pipeline", lambda db, config, **kw: original(db, config, sync_manager_factory=Garmin, **kw)
    )
    return handlers.make_handlers(config, {"runner": runner})["sync"](db, {})


def _grade_at(db: Database, activity_id: str) -> datetime.datetime | None:
    row = db.fetchone("SELECT grade_at FROM grade_waits WHERE activity_id = ?", [activity_id])
    return row[0] if row else None


# ---------------------------------------------------------------------------


def test_what_is_missing(db: Database) -> None:
    _activity(db, "ride", NOW)
    assert grade_wait.missing(db, "ride") == ["note", "effort"]
    db.execute("UPDATE activities SET description = '  ' WHERE activity_id = 'ride'")
    assert grade_wait.missing(db, "ride") == ["note", "effort"]  # blank isn't a note
    db.execute("UPDATE activities SET description = '3×10′ sweet spot', rpe = 60 WHERE activity_id = 'ride'")
    assert grade_wait.missing(db, "ride") == []
    db.execute("INSERT INTO session_feedback (activity_id, comment) VALUES ('ride', '')")  # cleared in hart
    assert grade_wait.missing(db, "ride") == ["note"]
    _activity(db, "gym", NOW, sport="strength", power=None)
    db.execute("INSERT INTO session_feedback (activity_id, feel) VALUES ('gym', 4)")
    assert grade_wait.missing(db, "gym") == ["note"]  # feel alone counts as effort


def test_sync_holds_sessions_without_a_note_until_it_arrives(db: Database, config, discord, monkeypatch) -> None:
    runner = Runner()
    Garmin.new = {
        "ride": {},  # nothing written yet: waits
        "noted": {"description": "Z2, legs fine", "rpe": 40},  # graded right away
        "walk": {"sport_type": "other", "sub_type": "generic"},  # never graded by Claude: no wait
    }
    result = _sync(db, config, runner, monkeypatch)
    assert sorted(runner.grades()) == [("noted", "sync"), ("walk", "sync")]
    assert result["waiting_for_notes"] == ["ride"]
    first = _grade_at(db, "ride")
    assert first is not None

    asked = discord["sent"][0]
    assert "No note or RPE/feel yet" in asked["content"] and "Ember grades with them" in asked["content"]
    buttons = asked["components"][0]["components"]
    assert [b.get("custom_id") for b in buttons] == ["hart:gradenote:ride", "hart:gradenow:ride", None]
    assert buttons[2]["url"] == "https://hart.example.ts.net/sessions/ride"

    # Later syncs neither restart the wait nor grade it while something is still missing.
    runner.calls.clear()
    _sync(db, config, runner, monkeypatch)
    db.execute("UPDATE activities SET description = 'Sweet spot 3×10′' WHERE activity_id = 'ride'")
    _sync(db, config, runner, monkeypatch)
    assert runner.grades() == [] and _grade_at(db, "ride") == first and len(discord["sent"]) == 1

    # Effort arrives too: graded with both, once; the message loses its buttons.
    db.execute("UPDATE activities SET rpe = 60 WHERE activity_id = 'ride'")
    _sync(db, config, runner, monkeypatch)
    assert runner.grades() == [("ride", "notes_added")] and _grade_at(db, "ride") is None
    message_id, edited = discord["edited"][0]
    assert message_id == "m1" and edited["components"] == [] and grade_wait.NOTES_IN in edited["content"]
    _sync(db, config, runner, monkeypatch)
    assert runner.grades() == [("ride", "notes_added")]  # an ungraded session's edits don't regrade it


def test_no_wait_when_turned_off(db: Database, config, discord, monkeypatch) -> None:
    settings.set_value(db, "grade_wait_minutes", 0)
    runner = Runner()
    Garmin.new = {"ride": {}}
    _sync(db, config, runner, monkeypatch)
    assert runner.grades() == [("ride", "sync")] and discord["sent"] == []


def test_webhook_gets_a_link_instead_of_buttons(db: Database, config, discord) -> None:
    webhook = dataclasses.replace(
        config, discord=dataclasses.replace(config.discord, bot_token="", webhook_url="https://x")
    )
    _activity(db, "ride", NOW)
    db.execute("UPDATE activities SET rpe = 50 WHERE activity_id = 'ride'")
    assert grade_wait.hold(db, webhook, ["ride"]) == ["ride"]
    asked = discord["sent"][0]
    assert "components" not in asked and asked["content"].endswith("https://hart.example.ts.net/sessions/ride")
    assert "No note yet" in asked["content"]
    assert db.fetchone("SELECT message_id FROM grade_waits")[0] is None  # nothing to edit later


def test_when_the_wait_is_over_a_sync_runs_first(db: Database, config, discord, monkeypatch) -> None:
    _activity(db, "ride", NOW)
    start = datetime.datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
    grade_wait.hold(db, config, ["ride"], now=start)  # 120 minutes by default
    end = start + 120 * MIN
    runner = Runner()
    scheduler = Scheduler(db, runner, "Europe/Warsaw", config)  # type: ignore[arg-type]
    assert scheduler.grade_waits_due(end - MIN) == [] and runner.calls == []

    # The wait is over: a sync, so a note added in the last minutes is graded with — only one.
    scheduler.grade_waits_due(end + MIN)
    scheduler.grade_waits_due(end + 2 * MIN)
    assert [t for t, _ in runner.calls] == ["sync"] and _grade_at(db, "ride") is not None

    # That sync didn't grade it (it never ran): graded without after the grace period.
    scheduler.grade_waits_due(end + 11 * MIN)
    assert runner.grades() == [("ride", "sync")] and _grade_at(db, "ride") is None
    assert grade_wait.WAIT_OVER in discord["edited"][0][1]["content"]


def test_a_sync_already_running_is_not_enough(db: Database, config, discord) -> None:
    _activity(db, "ride", NOW)
    start = datetime.datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
    grade_wait.hold(db, config, ["ride"], now=start)
    runner = Runner(sync_status="already_running")  # may have fetched before the wait ended
    scheduler = Scheduler(db, runner, "Europe/Warsaw", config)  # type: ignore[arg-type]
    scheduler.grade_waits_due(start + 121 * MIN)
    scheduler.grade_waits_due(start + 122 * MIN)
    assert [t for t, _ in runner.calls] == ["sync", "sync"]

    # Garmin blocked: no point waiting for a sync.
    state.set_setting(db, state.GARMIN_AUTH_PAUSED, True)
    scheduler.grade_waits_due(start + 123 * MIN)
    assert runner.grades() == [("ride", "sync")]


def test_the_sync_grades_waits_that_are_over(db: Database, config, discord, monkeypatch) -> None:
    _activity(db, "ride", NOW)
    grade_wait.hold(db, config, ["ride"], now=datetime.datetime.now(tz=UTC) - 3 * 60 * MIN)
    runner = Runner()
    _sync(db, config, runner, monkeypatch)
    assert runner.grades() == [("ride", "sync")] and _grade_at(db, "ride") is None


def test_sweep_leaves_waiting_sessions_alone(db: Database, config, discord) -> None:
    _activity(db, "ride", NOW - datetime.timedelta(hours=1))
    grade_wait.hold(db, config, ["ride"])
    assert ungraded_recent(db) == []
    db.execute("UPDATE grade_waits SET grade_at = ?", [datetime.datetime.now(tz=UTC) - MIN])
    assert ungraded_recent(db) == ["ride"]


def test_discord_buttons_and_the_note_form(db: Database, config, discord) -> None:
    _activity(db, "ride", NOW)
    _activity(db, "run", NOW, sport="run", power=None)
    db.execute("UPDATE activities SET rpe = 60 WHERE activity_id = 'ride'")  # RPE on the watch, no note
    grade_wait.hold(db, config, ["ride", "run"])
    runner = Runner()
    bot = discord_chat.DiscordChat(db, chat=None, config=config, enqueue=runner.enqueue)  # type: ignore[arg-type]

    assert bot.handle_interaction(3, STRANGER, {"custom_id": "hart:gradenow:ride"})["type"] == 4
    assert bot.handle_interaction(3, ME, {"custom_id": "hart:gradenow:nope"})["type"] == 4
    assert runner.calls == []

    form = bot.handle_interaction(3, ME, {"custom_id": "hart:gradenote:ride"})
    assert form["type"] == 9 and form["data"]["custom_id"] == "hart:gradenote:ride"
    fields = {row["components"][0]["custom_id"]: row["components"][0] for row in form["data"]["components"]}
    assert fields["rpe"]["value"] == "6" and "value" not in fields["note"]  # prefilled from Garmin
    assert all(len(f["label"]) <= 45 for f in fields.values()) and len(form["data"]["title"]) <= 45

    def submit(rpe: str, feel: str, note: str) -> dict[str, Any]:
        rows = [
            {"type": 1, "components": [{"type": 4, "custom_id": k, "value": v}]}
            for k, v in (("rpe", rpe), ("feel", feel), ("note", note))
        ]
        return bot.handle_interaction(5, ME, {"custom_id": "hart:gradenote:ride", "components": rows})

    bad = submit("11", "4", "3×10′ sweet spot")
    assert bad["type"] == 4 and "RPE" in bad["data"]["content"] and grade_wait.is_waiting(db, "ride")
    done = submit("7", "4", "3×10′ sweet spot, legs heavy")
    assert done["type"] == 7 and done["data"]["components"] == [] and grade_wait.SAVED in done["data"]["content"]
    assert db.fetchone("SELECT rpe, feel, comment FROM session_feedback WHERE activity_id = 'ride'") == (
        7,
        4,
        "3×10′ sweet spot, legs heavy",
    )
    assert runner.grades() == [("ride", "notes_added")] and not grade_wait.is_waiting(db, "ride")

    skipped = bot.handle_interaction(3, ME, {"custom_id": "hart:gradenow:run"})
    assert skipped["type"] == 7 and grade_wait.SKIPPED in skipped["data"]["content"]
    assert runner.grades()[-1] == ("run", "sync")
    again = bot.handle_interaction(3, ME, {"custom_id": "hart:gradenow:run"})
    assert again["type"] == 7 and len(runner.grades()) == 2  # no second grade
    assert discord["edited"] == []  # the clicks answered with the new message themselves


def test_form_values_in_rows_or_labels() -> None:
    labelled = {"components": [{"type": 18, "component": {"type": 4, "custom_id": "note", "value": "ok"}}]}
    assert grade_wait._form_values(labelled) == {"note": "ok"}


def test_page_shows_the_wait_and_any_grade_ends_it(client) -> None:  # noqa: F811
    config = client.app.state.config
    with client.app.state.db.cursor() as cur:
        assert grade_wait.hold(cur, config, ["ride", "old1"]) == ["ride", "old1"]  # no Discord: still waits
    page = client.get("/sessions/ride", headers=H).text
    assert "Waiting for your note and RPE/feel" in page
    listing = client.get("/api/sessions", headers=H).json()["items"]
    assert next(s for s in listing if s["activity_id"] == "ride")["grade"]["waiting"] is True

    # Feedback saved on the page: graded now, with it.
    FakeClient.scripts.extend([[_result(structured_output=GRADE)] for _ in range(2)])
    fb = client.post("/api/sessions/ride/feedback", headers=W, json={"rpe": 5, "note": "easy spin"}).json()
    job = _wait_job(client, fb["regrade"]["job_id"])
    assert job["status"] == "ok" and job["payload"]["trigger"] == "notes_added"
    assert (
        json.loads(json.dumps(client.get("/api/sessions/ride/grade", headers=H).json()["features"]))["feedback"]["note"]
        == "easy spin"
    )

    # "Grade now" on the page ends the wait too.
    job = client.post("/api/sessions/old1/grade", headers=W, json={}).json()
    _wait_job(client, job["job_id"])
    with client.app.state.db.cursor() as cur:
        assert cur.fetchall("SELECT * FROM grade_waits") == []
