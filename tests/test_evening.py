"""Evening Discord message (after 22:00)."""

from __future__ import annotations

import dataclasses
import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.test_grading import _activity
from hart.config import get_config
from hart.server import evening, settings, state, suggestions
from hart.storage.database import Database

TZ = ZoneInfo("Europe/Warsaw")


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "e.duckdb").connect()
    yield database
    database.close()


def _config(webhook: str = "https://discord.example/webhook") -> Any:
    base = get_config()
    return dataclasses.replace(base, discord=dataclasses.replace(base.discord, webhook_url=webhook, bot_token=""),
                               server=dataclasses.replace(base.server, public_host="hart.example.ts.net"))


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
    db.execute("INSERT INTO jobs (type, status, dedupe_key, trigger, payload) VALUES "
               "('suggest', 'running', ?, 'schedule', '{}')", [f"suggest:{tomorrow}"])
    assert not evening.due(db, _config(), _at(22, 10))
    assert evening.due(db, _config(), _at(22, 31))  # doesn't wait forever


def test_message_content_and_send(db: Database, monkeypatch) -> None:
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    _activity(db, "ride", datetime.datetime.combine(today, datetime.time(9)), secs=3660)
    db.execute("INSERT INTO planned_sessions (date, sport_type, title, duration_min, source) "
               "VALUES (?, 'run', 'Coach easy run', 40, 'coach_import')", [tomorrow])
    suggestions.store(db, tomorrow, {
        "kind": "preliminary", "readiness": "green", "status": "ok", "recommendation": "modify",
        "summary": "Keep it shorter after today's ride.",
        "sessions": [{"sport_type": "run", "title": "Easy run", "duration_min": 30, "intensity": "endurance"}],
        "cautions": ["Calf: stop if it tightens"],
    })
    sent: list[dict[str, Any]] = []

    class Response:
        status_code = 204
        text = ""

    import httpx
    monkeypatch.setattr(httpx, "post", lambda url, **kw: sent.append({"url": url, **kw}) or Response())
    out = evening.run(db, _config())
    text = sent[0]["json"]["content"]
    assert out["sent"] and sent[0]["url"] == "https://discord.example/webhook"
    assert "Coach:** Coach easy run (40′)" in text and "modify the coach's session" in text
    assert "• Easy run — 30′ endurance" in text and "Calf" in text
    assert "**Today:** Ride 1:01 h" in text and "<https://hart.example.ts.net/plan>" in text
    assert sent[0]["json"]["allowed_mentions"] == {"parse": []}
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
