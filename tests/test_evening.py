"""Evening Discord message (after 22:00)."""

from __future__ import annotations

import dataclasses
import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from hart.config import get_config
from hart.server import evening, settings, state, suggestions
from hart.storage.database import Database
from tests.test_grading import _activity

TZ = ZoneInfo("Europe/Warsaw")


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "e.duckdb").connect()
    yield database
    database.close()


def _config(webhook: str = "https://discord.example/webhook") -> Any:
    base = get_config()
    return dataclasses.replace(
        base,
        discord=dataclasses.replace(base.discord, webhook_url=webhook, bot_token=""),
        server=dataclasses.replace(base.server, public_host="hart.example.ts.net"),
    )


def _at(hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime.combine(datetime.date.today(), datetime.time(hour, minute), TZ)


def test_due_rules(db: Database) -> None:
    config = _config()
    assert not evening.due(db, config, _at(21, 59))
    assert evening.due(db, config, _at(22, 1))
    assert not evening.due(db, _config(webhook=""), _at(22, 1))  # not configured
    settings.set_value(db, "evening_message_enabled", False)
    assert not evening.due(db, config, _at(22, 1))
    settings.set_value(db, "evening_message_enabled", True)
    state.set_setting(db, evening.SENT_KEY, datetime.date.today().isoformat())
    assert not evening.due(db, config, _at(23, 0))  # once a day
    state.delete_setting(db, evening.SENT_KEY)
    state.set_setting(db, evening.FAILED_KEY, _at(22, 5).isoformat())
    assert not evening.due(db, config, _at(22, 20)) and evening.due(db, config, _at(22, 40))


def test_waits_for_tomorrows_suggestion(db: Database) -> None:
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    db.execute(
        "INSERT INTO jobs (type, status, dedupe_key, trigger, payload) VALUES "
        "('suggest', 'running', ?, 'schedule', '{}')",
        [f"suggest:{tomorrow}"],
    )
    assert not evening.due(db, _config(), _at(22, 10))
    assert evening.due(db, _config(), _at(22, 31))  # doesn't wait forever


def test_message_content_and_send(db: Database, monkeypatch) -> None:
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    _activity(db, "ride", datetime.datetime.combine(today, datetime.time(9)), secs=3660)
    db.execute(
        "INSERT INTO planned_sessions (date, sport_type, title, duration_min, source) "
        "VALUES (?, 'run', 'Coach easy run', 40, 'coach_import')",
        [tomorrow],
    )
    suggestions.store(
        db,
        tomorrow,
        {
            "kind": "preliminary",
            "readiness": "green",
            "status": "ok",
            "recommendation": "modify",
            "summary": "Keep it shorter after today's ride.",
            "sessions": [{"sport_type": "run", "title": "Easy run", "duration_min": 30, "intensity": "endurance"}],
            "cautions": ["Calf: stop if it tightens"],
        },
    )
    sent: list[dict[str, Any]] = []

    class Response:
        status_code = 204
        text = ""

    import httpx

    monkeypatch.setattr(httpx, "post", lambda url, **kw: sent.append({"url": url, **kw}) or Response())
    out = evening.run(db, _config())
    body = sent[0]["json"]
    embed = body["embeds"][0]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert out["sent"] and sent[0]["url"] == "https://discord.example/webhook"
    assert "content" not in body  # the real message is the embed alone
    assert embed["title"].startswith("🌲 Tomorrow · ") and embed["url"] == "https://hart.example.ts.net/plan"
    assert embed["color"] == evening.READINESS["green"][0]
    assert "**Adjusted**" in embed["description"] and "Keep it shorter" in embed["description"]
    assert fields["🏃 Easy run"] == "30′ · endurance"
    assert fields["📋 Coach's plan"] == "Coach easy run · 40′"
    assert fields["⚠️ Watch out"] == "• Calf: stop if it tightens"
    assert fields["Today"].startswith("🚴 Ride · 1:01 h")
    assert body["allowed_mentions"] == {"parse": []}
    assert state.get_setting(db, evening.SENT_KEY) == today.isoformat()


def test_failed_send_is_recorded(db: Database, monkeypatch) -> None:
    class Response:
        status_code = 401
        text = "Invalid Webhook Token"

    import httpx

    monkeypatch.setattr(httpx, "post", lambda url, **kw: Response())
    with pytest.raises(evening.DiscordError):
        evening.run(db, _config())
    assert state.get_setting(db, evening.FAILED_KEY) and not state.get_setting(db, evening.SENT_KEY)


def test_test_message_and_alerts(db: Database, monkeypatch) -> None:
    sent: list[dict[str, Any]] = []

    class Response:
        status_code = 204
        text = ""

    import httpx

    monkeypatch.setattr(httpx, "post", lambda url, **kw: sent.append(kw["json"]) or Response())
    monkeypatch.setattr(
        evening, "alerts", lambda db, config: {"garmin_blocked": None, "health_overdue": 1, "proposed_notes": 2}
    )
    evening.run(db, _config(), test=True)
    body = sent[0]
    assert body["content"].startswith("🧪 Test")
    embed = body["embeds"][0]
    assert embed["description"] == "No suggestion for tomorrow yet."
    assert embed["footer"]["text"] == "🔔 1 health check due · 2 notes waiting for approval"
    assert {"name": "Today", "value": "Rest day", "inline": True} in embed["fields"]
    assert not state.get_setting(db, evening.SENT_KEY)  # a test doesn't count as tonight's message
