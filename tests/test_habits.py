"""Sauna log and daily habits: analytics, storage, the Discord check-in, MCP tools and the Health page."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
from pathlib import Path
from typing import Any

import pytest

from hart.analytics.habits import habit_key, next_morning_comparison, parse_habits, sauna_summary
from hart.config import get_config
from hart.server import discord_chat, evening, habits, settings
from hart.storage.database import Database
from tests.test_grading import H, W, client  # noqa: F401 — fixture

D = datetime.date
DAY = datetime.timedelta(days=1)
ME, STRANGER = 42, 99


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "h.duckdb").connect()
    settings.set_value(database, "habits", "💊 Supplements, 🌙 Late meal, 🍷 Alcohol")
    yield database
    database.close()


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


def test_habit_keys_and_list() -> None:
    assert habit_key("🌙 Late meal") == "late_meal"
    assert habit_key("Późny posiłek") == "pozny_posilek"
    parsed = parse_habits("💊 Supplements,\n🌙 Late meal, supplements, 🧖 Sauna, , Stretch")
    assert parsed == [
        {"key": "supplements", "label": "💊 Supplements"},
        {"key": "late_meal", "label": "🌙 Late meal"},
        {"key": "stretch", "label": "Stretch"},
    ]  # duplicates, blanks and sauna (it has its own log) are dropped


def test_sauna_summary() -> None:
    today = D(2026, 10, 4)
    sessions = [{"date": today - n * DAY, "duration_min": 15} for n in (0, 3, 10, 27, 28)]
    s = sauna_summary(sessions, today)
    assert s["last_7_days"] == {"sessions": 2, "minutes": 30}
    assert s["last_28_days"] == {"sessions": 4, "minutes": 60, "per_week": 1.0}
    assert s["last_session"] == today and s["days_since_last"] == 0
    assert sauna_summary([], today)["last_session"] is None


def test_next_morning_comparison_uses_the_following_night() -> None:
    start = D(2026, 9, 1)
    observed = [start + i * DAY for i in range(10)]
    events = {observed[i] for i in (0, 2, 4, 6, 8)}
    # The morning after an event day has HRV 50, other mornings 60; resting HR only on 2 nights.
    outcomes = {d + DAY: {"hrv_last_night_ms": 50 if d in events else 60} for d in observed}
    outcomes[observed[1] + DAY]["resting_hr"] = 48
    outcomes[observed[2] + DAY]["resting_hr"] = 50
    out = next_morning_comparison(events, observed, outcomes)
    assert out["days_with"] == 5 and out["days_without"] == 5
    hrv = out["metrics"]["hrv_last_night_ms"]
    assert hrv["with"] == {"nights": 5, "mean": 50.0} and hrv["without"] == {"nights": 5, "mean": 60.0}
    assert hrv["difference"] == -10.0 and hrv["higher_is_better"]
    assert out["metrics"]["resting_hr"]["difference"] is None  # too few nights to compare


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_sauna_validation_and_update(db: Database) -> None:
    day = D(2026, 10, 3)
    s = habits.log_sauna(db, day, "web", duration_min="20 min", temperature_c="85°C", rounds=3, kind="Fin")
    assert (s["duration_min"], s["temperature_c"], s["rounds"], s["kind"]) == (20, 85.0, 3, "finnish")
    for bad in ({"duration_min": None}, {"duration_min": 500}, {"duration_min": 10, "kind": "sauna bath"}):
        with pytest.raises(habits.HabitError):
            habits.log_sauna(db, day, "web", **bad)
    updated = habits.update_sauna(db, s["id"], duration_min=25, start_time="19:30")
    assert updated["duration_min"] == 25 and updated["start_time"] == "19:30" and updated["temperature_c"] is None
    habits.delete_sauna(db, s["id"])
    assert habits.sauna_sessions(db, day, day) == []


def test_answering_one_habit_answers_the_day(db: Database) -> None:
    day = D(2026, 10, 3)
    assert habits.day_habits(db, day) == {}  # unknown
    habits.set_habits(db, day, {"late_meal": True}, "ember")
    assert habits.day_habits(db, day) == {"supplements": False, "late_meal": True, "alcohol": False}
    habits.set_habits(db, day, {"supplements": True}, "web")  # later answers keep the earlier ones
    assert habits.day_habits(db, day)["late_meal"] is True
    with pytest.raises(habits.HabitError):
        habits.set_habits(db, day, {"coffee": True}, "web")
    assert (
        habits.resolve_habit(db, "Late Meal") == "late_meal" and habits.resolve_habit(db, "Supplement") == "supplements"
    )


def test_context_and_comparisons(db: Database) -> None:
    today = D(2026, 10, 4)
    for i in range(1, 13):
        d = today - i * DAY
        if i % 2:
            habits.log_sauna(db, d, "web", duration_min=15)
        habits.set_habits(db, d, {"alcohol": i % 3 == 0}, "web")
        db.execute("INSERT INTO hrv_daily (date, hrv_last_night_ms) VALUES (?, ?)", [d + DAY, 50 if i % 2 else 60])
    effects = habits.comparisons(db, today)
    assert effects["sauna"]["metrics"]["hrv_last_night_ms"]["difference"] == -10.0
    assert effects["alcohol"]["days_with"] == 4 and effects["alcohol"]["label"] == "🍷 Alcohol"

    ctx = habits.recent_for_context(db, today)
    assert [d["date"] for d in ctx["days"]] == [today - DAY, today - 2 * DAY, today - 3 * DAY]
    assert ctx["days"][0]["sauna"] == [{"duration_min": 15}] and ctx["days"][2]["habits_done"] == ["🍷 Alcohol"]


# ---------------------------------------------------------------------------
# Discord check-in
# ---------------------------------------------------------------------------


def test_checkin_buttons_toggle_habits(db: Database) -> None:
    day = D(2026, 10, 4)
    msg = habits.checkin_message(db, day)
    ids = [b["custom_id"] for row in msg["components"] for b in row["components"]]
    assert ids == [
        "hart:habit:2026-10-04:supplements",
        "hart:habit:2026-10-04:late_meal",
        "hart:habit:2026-10-04:alcohol",
        "hart:none:2026-10-04",
    ]
    labels = [b["label"] for row in msg["components"] for b in row["components"]]
    assert labels == ["💊 Supplements", "🌙 Late meal", "🍷 Alcohol", "None of these"]
    assert "nothing is saved" in msg["content"]

    click = habits.handle_interaction(db, 3, {"custom_id": "hart:habit:2026-10-04:late_meal"})
    assert click["type"] == 7 and "Logged: 🌙 Late meal" in click["data"]["content"]
    assert habits.day_habits(db, day) == {"supplements": False, "late_meal": True, "alcohol": False}
    late = next(b for row in click["data"]["components"] for b in row["components"] if "late_meal" in b["custom_id"])
    assert late["style"] == habits.SUCCESS and late["label"].endswith("✓")

    habits.handle_interaction(db, 3, {"custom_id": "hart:habit:2026-10-04:late_meal"})  # toggled off again
    assert habits.day_habits(db, day)["late_meal"] is False
    none = habits.handle_interaction(db, 3, {"custom_id": "hart:none:2026-10-04"})
    assert "Logged: none of these" in none["data"]["content"]
    gone = habits.handle_interaction(db, 3, {"custom_id": "hart:habit:2026-10-04:coffee"})
    assert gone["type"] == 4 and gone["data"]["flags"] == 64
    assert habits.handle_interaction(db, 3, {"custom_id": "other:thing"}) is None

    settings.set_value(db, "habits", "")  # no habits: no check-in, and old buttons say why
    assert habits.checkin_message(db, day) is None
    assert habits.handle_interaction(db, 3, {"custom_id": "hart:none:2026-10-04"})["type"] == 4


class Channel:
    def __init__(self, id: int, type: str = "private") -> None:
        self.id, self.type, self.sent = id, type, []

    async def send(self, text: str) -> None:
        self.sent.append(text)


def test_bot_routes_buttons_and_the_habits_command(db: Database, monkeypatch) -> None:
    base = get_config()
    config = dataclasses.replace(
        base, discord=dataclasses.replace(base.discord, bot_token="t", channel_id=111, allowed_user_ids=(ME,))
    )
    bot = discord_chat.DiscordChat(db, chat=None, config=config)  # type: ignore[arg-type]
    stranger = bot.handle_interaction(3, STRANGER, {"custom_id": "hart:none:2026-10-04"})
    assert stranger["type"] == 4 and habits.day_habits(db, D(2026, 10, 4)) == {}
    assert bot.handle_interaction(3, ME, {"custom_id": "hart:none:2026-10-04"})["type"] == 7
    assert bot.handle_interaction(3, ME, {"custom_id": "someone_else"}) is None

    posted: list[tuple[dict[str, Any], Any]] = []
    monkeypatch.setattr(evening, "send", lambda cfg, message, channel_id=None: posted.append((message, channel_id)))
    message = type(
        "Message", (), {"author": type("A", (), {"id": ME, "bot": False})(), "webhook_id": None, "mentions": []}
    )()
    message.channel, message.content = Channel(500), "Habits yesterday"
    asyncio.run(bot.handle(message))
    assert posted[0][1] == 500 and "Habits ·" in posted[0][0]["content"] and not message.channel.sent
    yesterday = discord_chat.local_today(config) - DAY
    payload = json.dumps(posted[0][0])
    assert f"hart:none:{yesterday.isoformat()}" in payload and "sauna" not in payload


def test_checkin_comes_before_the_evening_message(db: Database, monkeypatch) -> None:
    from zoneinfo import ZoneInfo

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(evening, "send", lambda config, message, channel_id=None: sent.append(message) or {})
    base = get_config()
    bot = dataclasses.replace(base, discord=dataclasses.replace(base.discord, bot_token="t", channel_id=111))
    today = discord_chat.local_today(bot)

    def at(hour: int, minute: int = 0) -> datetime.datetime:
        return datetime.datetime.combine(today, datetime.time(hour, minute), ZoneInfo(bot.server.tz))

    assert not evening.checkin_due(db, bot, at(21, 29)) and evening.checkin_due(db, bot, at(21, 31))
    assert evening.run_checkin(db, bot) == {"sent": True}
    assert sent[0]["components"] and "Sauna" not in json.dumps(sent[0], ensure_ascii=False)
    assert not evening.checkin_due(db, bot, at(23, 0))  # once a day

    # The summary at 22:00 shows what was answered — and that it's still open when nothing was.
    habits.log_sauna(db, today, "web", duration_min=18, temperature_c=80)
    evening.run(db, bot)
    summary = sent[-1]["embeds"][0]
    assert summary["description"] == "Rest day — no training\n\n🧖 Sauna · 18′ · 80 °C"
    habits_field = next(f for f in summary["fields"] if f["name"] == "✅ Habits")
    assert habits_field["value"].startswith("Not answered yet")
    habits.set_habits(db, today, {"supplements": True}, "discord")
    assert {"name": "✅ Habits", "value": "💊 Supplements", "inline": True} in evening.day_summary(db, bot, today)[
        "fields"
    ]

    webhook = dataclasses.replace(
        base, discord=dataclasses.replace(base.discord, bot_token="", webhook_url="https://x")
    )
    assert not evening.checkin_due(db, webhook, at(21, 31))  # webhooks can't have buttons
    settings.set_value(db, "habits", "")
    assert not evening.checkin_due(db, bot, at(21, 31)) and evening.run_checkin(db, bot)["sent"] is False


# ---------------------------------------------------------------------------
# Ember's tools and the Health page
# ---------------------------------------------------------------------------


def test_policy_lets_chat_log_but_not_read_only_runs() -> None:
    from hart.server.claude.policy import chat_policy, read_only_policy

    for tool in ("log_sauna_session", "update_sauna_session", "log_habits"):
        assert tool in chat_policy(web=False).allowed_mcp and tool not in read_only_policy().allowed_mcp
    assert "get_habits" in read_only_policy().allowed_mcp


def test_several_visits_a_day(db: Database) -> None:
    """2 rounds in the morning and 3 in the evening: two sessions, described for Ember's confirmation."""
    from hart import mcp_server

    mcp_server.configure_server(db, lambda *a, **k: {}, lambda _id: None)
    try:
        json.loads(mcp_server.log_sauna_session(16, date="2026-10-03", rounds=2, notes="morning"))
        out = json.loads(
            mcp_server.log_sauna_session(24, date="2026-10-03", rounds=3, temperature_c=90, start_time="19:00")
        )
    finally:
        mcp_server.configure_server(None, None, None)  # type: ignore[arg-type]
    assert [s["text"] for s in out["day"]["sessions"]] == ["19:00 · 3 rounds · 24′ · 90 °C", "2 rounds · 16′"]
    day = D(2026, 10, 3)
    assert habits.sauna_summary(habits.sauna_sessions(db, day, day), day)["last_7_days"] == {
        "sessions": 2,
        "minutes": 40,
    }
    moved = habits.update_sauna(db, out["session"]["id"], date=day - DAY, duration_min=20)
    assert moved["date"] == day - DAY and moved["rounds"] is None


def test_mcp_tools_and_health_page(client) -> None:  # noqa: F811
    from hart import mcp_server

    assert client.put("/api/settings/habits", headers=W, json={"value": "💊 Supplements, 🌙 Late meal"}).is_success
    out = json.loads(mcp_server.log_sauna_session(20, date="2026-10-03", temperature_c=85, kind="infrared"))
    assert out["status"] == "saved" and out["session"]["kind"] == "infrared"
    assert "error" in json.loads(mcp_server.log_sauna_session(0))
    logged = json.loads(mcp_server.log_habits(["late meal"], date="2026-10-03"))
    assert logged["habits"] == {"supplements": False, "late_meal": True}
    assert "unknown habit" in json.loads(mcp_server.log_habits(["coffee"]))["error"]
    fixed = json.loads(mcp_server.update_sauna_session(out["session"]["id"], 25, temperature_c=85))
    assert fixed["session"]["duration_min"] == 25 and fixed["session"]["kind"] is None

    got = json.loads(mcp_server.get_habits("2026-10-01", "2026-10-04"))
    assert [s["duration_min"] for s in got["sauna_sessions"]] == [25]
    assert [d["date"] for d in got["days"]] == ["2026-10-03"] and "sauna" in got["next_morning"]

    page = client.get("/health", headers=H).text
    assert "Heat &amp; habits" in page and "🌙 Late meal" in page and "25′" in page
    assert client.post(
        "/api/habits", headers=W, json={"date": "2026-10-02", "habit": "supplements", "done": True}
    ).is_success
    added = client.post("/api/sauna", headers=W, json={"date": "2026-10-02", "duration_min": 12}).json()
    edited = client.put(
        f"/api/sauna/{added['id']}", headers=W, json={"date": "2026-10-01", "duration_min": 14, "rounds": 2}
    )
    assert (edited.json()["date"], edited.json()["duration_min"], edited.json()["rounds"]) == ("2026-10-01", 14, 2)
    assert "editSauna(" in client.get("/health", headers=H).text
    assert client.post("/api/sauna", headers=W, json={"date": "2026-10-02", "duration_min": 0}).status_code == 422
    data = client.get("/api/habits", headers=H).json()
    assert data["definitions"][0]["key"] == "supplements"
    assert client.request("DELETE", f"/api/sauna/{out['session']['id']}", headers=W, json={}).is_success
    assert client.request("DELETE", "/api/habits/2026-10-02", headers=W, json={}).is_success
    assert json.loads(mcp_server.update_sauna_session(out["session"]["id"], 1, delete=True))["error"]


def test_day_summary_has_sessions_body_form_sauna_and_habits(db: Database) -> None:
    from tests.test_grading import _activity

    base = get_config()
    config = dataclasses.replace(base, server=dataclasses.replace(base.server, public_host=""))
    day = D(2026, 10, 3)
    _activity(db, "ride", datetime.datetime(2026, 10, 3, 9), secs=3720, hr=128, power=182)
    _activity(db, "run", datetime.datetime(2026, 10, 3, 17), sport="run", sub="road", secs=1800, hr=141, power=None)
    db.execute("UPDATE activities SET name = 'Easy run', distance_meters = 5230 WHERE activity_id = 'run'")
    db.execute("INSERT INTO sleep_records (date, total_sleep_sec, sleep_score) VALUES (?, 26000, 82)", [day])
    db.execute("INSERT INTO hrv_daily (date, hrv_last_night_ms) VALUES (?, 58)", [day])
    db.execute("INSERT INTO daily_health (date, resting_hr, steps) VALUES (?, 47, 9412)", [day])
    db.execute(
        "INSERT INTO daily_training_load (date, sport_type, ctl, atl, tsb) VALUES (?, 'combined', 42.4, 50.2, -7.8)",
        [day],
    )
    habits.log_sauna(db, day, "ember", duration_min=16, rounds=2, notes="morning")
    habits.log_sauna(db, day, "ember", duration_min=24, rounds=3, start_time="19:00", temperature_c=90)
    habits.set_habits(db, day, {"supplements": True}, "discord")

    embed = evening.day_summary(db, config, day)
    assert embed["title"] == "📒 Today · Saturday 3 Oct" and "url" not in embed
    assert embed["description"].splitlines() == [
        "🚴 **Ride** · 1:02 h · ♥ 128 · 182 W · load 40",
        "🏃 **Easy run** · 30′ · 5.2 km · ♥ 141 · load 40",
        "Σ 2 sessions · 1:32 h · load 80",
        "",
        "🧖 Sauna · 19:00 · 3 rounds · 24′ · 90 °C",
        "🧖 Sauna · 2 rounds · 16′",
    ]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["😴 Last night"] == "7:13 h · score 82\nHRV 58 ms"
    assert fields["❤️ Body"] == "Resting HR 47\n9 412 steps"
    assert fields["📈 Form"] == "Fitness 42 · Fatigue 50\nForm -8"
    assert fields["✅ Habits"] == "💊 Supplements"


def test_grading_sees_sauna_and_last_nights_habits(db: Database) -> None:
    from hart.analytics.grading_features import heat_and_habits

    start = datetime.datetime(2026, 10, 3, 10)
    assert heat_and_habits(db, start) is None
    habits.log_sauna(db, D(2026, 10, 2), "ember", duration_min=20, start_time="21:30")
    habits.log_sauna(db, D(2026, 10, 3), "ember", duration_min=12, start_time="09:15")
    habits.log_sauna(db, D(2026, 10, 3), "ember", duration_min=15)
    habits.set_habits(db, D(2026, 10, 2), {"late_meal": True}, "discord")
    out = heat_and_habits(db, start)
    assert [(s["when"], s["duration_min"]) for s in out["sauna"]] == [
        ("day before", 20),
        ("before the session", 12),
        ("same day, time unknown", 15),
    ]
    assert out["habits_done_day_before"] == ["late_meal"]
