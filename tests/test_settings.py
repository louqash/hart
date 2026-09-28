"""Runtime settings: registry resolution, the Settings page/API, and where settings take effect."""

from __future__ import annotations

import dataclasses
import datetime
from pathlib import Path

import pytest

from tests.test_chat import FakeClient
from tests.test_grading import H, W
from hart.config import ServerSettings, get_config
from hart.server import settings
from hart.server.claude.prompts import chat_system_prompt
from hart.storage.database import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "s.duckdb").connect()
    yield database
    database.close()


def test_resolution_order(db: Database, monkeypatch) -> None:
    assert settings.get(db, "athlete_name") == "Athlete"  # default
    monkeypatch.setenv("HART_ATHLETE_NAME", "Ada")
    assert settings.get(db, "athlete_name") == "Ada"  # env
    settings.set_value(db, "athlete_name", "Grace")
    assert settings.get(db, "athlete_name") == "Grace"  # the Settings page wins
    settings.reset(db, "athlete_name")
    assert settings.get(db, "athlete_name") == "Ada"
    monkeypatch.delenv("HART_ATHLETE_NAME")
    monkeypatch.setenv("TRI_MODEL_CHAT", "claude-sonnet-5")  # pre-rename name still works
    assert settings.get(db, "model_chat") == "claude-sonnet-5"
    monkeypatch.setenv("HART_PRELIMINARY_AT", "not a time")  # a broken env value falls back to the default
    assert settings.get(db, "preliminary_time") == "20:00"


def test_validation(db: Database) -> None:
    with pytest.raises(settings.SettingError):
        settings.set_value(db, "model_chat", "gpt-4")
    with pytest.raises(settings.SettingError):
        settings.set_value(db, "hourly_first", 30)
    assert settings.set_value(db, "preliminary_time", "21:5") == "21:05"
    assert settings.set_value(db, "power_single_sided", "yes") is True


def test_legacy_keys_are_respected(db: Database) -> None:
    from hart.server import state

    state.set_setting(db, "garmin_auto_send", False)  # stored before the registry existed
    assert settings.get(db, "garmin_auto_send") is False
    settings.set_value(db, "garmin_auto_send", True)
    assert settings.get(db, "garmin_auto_send") is True and state.get_setting(db, "garmin_auto_send") is None


def test_profile_reaches_prompts() -> None:
    prompt = chat_system_prompt(datetime.date(2027, 1, 1), "UTC",
                                {"name": "Ada", "power_single_sided": True, "has_coach": True})
    assert "You are Ember" in prompt and "Ada" in prompt and "single-sided" in prompt and "has a coach" in prompt
    solo = chat_system_prompt(datetime.date(2027, 1, 1), "UTC",
                              {"name": "Ada", "power_single_sided": False, "has_coach": False})
    assert "single-sided" not in solo and "no coach" in solo


def test_settings_page_and_api(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from hart.server.app import create_app

    config = dataclasses.replace(get_config(), db_path=tmp_path / "a.duckdb",
                                 server=ServerSettings(env="production", seed_dir=tmp_path / "seed"))
    with TestClient(create_app(config, run_scheduler=False, claude_client_factory=FakeClient),
                    base_url="https://hart.example.ts.net") as client:
        page = client.get("/settings", headers=H).text
        assert "Your name" in page and "Advanced thresholds" in page and "HART_AUTH_HEADER" in page
        assert client.put("/api/settings/athlete_name", headers=W, json={"value": "Ada"}).json()["value"] == "Ada"
        assert client.put("/api/settings/model_chat", headers=W, json={"value": "nope"}).status_code == 400
        assert client.put("/api/settings/db_path", headers=W, json={"value": "/tmp/x"}).status_code == 400
        assert client.put("/api/thresholds/training/ready_sleep_red_h", headers=W, json={"value": 5.5}).status_code == 200
        from hart.server import state

        with client.app.state.db.cursor() as cur:
            assert state.get_thresholds(cur)["ready_sleep_red_h"] == 5.5
            assert settings.get(cur, "athlete_name") == "Ada"
        client.request("DELETE", "/api/settings/athlete_name", headers=W, json={})
        assert "Athlete" in client.get("/settings", headers=H).text


def test_schedule_follows_settings(db: Database) -> None:
    from zoneinfo import ZoneInfo

    from tests.test_server import RecordingRunner
    from hart.server.jobs.scheduler import Scheduler

    tz = ZoneInfo("Europe/Warsaw")
    runner = RecordingRunner()
    sched = Scheduler(db, runner, "Europe/Warsaw")  # type: ignore[arg-type]
    settings.set_value(db, "preliminary_time", "21:30")
    sched.suggestions_due(datetime.datetime(2026, 9, 26, 21, 0, tzinfo=tz))
    assert runner.calls == []
    sched.suggestions_due(datetime.datetime(2026, 9, 26, 21, 31, tzinfo=tz))
    assert runner.calls == ["suggest"]


def test_paths(monkeypatch, tmp_path: Path) -> None:
    from hart import config as cfg

    for key in ("HART_DB_PATH", "TRI_DB_PATH", "DATABASE_PATH", "HART_SEED_DIR", "TRI_SEED_DIR", "GARMIN_TOKEN_PATH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HART_DATA_DIR", str(tmp_path))
    c = cfg.get_config(reload=True)
    assert c.db_path == tmp_path / "hart.duckdb" and c.server.seed_dir == tmp_path / "seed"
    assert c.garmin_token_path == tmp_path / ".garmin_tokens"
    (tmp_path / "triathlon.duckdb").touch()  # a database from before the rename is picked up
    assert cfg.get_config(reload=True).db_path == tmp_path / "triathlon.duckdb"
    monkeypatch.setenv("HART_DB_PATH", "data/dev.duckdb")  # explicit: relative to the project folder
    assert cfg.get_config(reload=True).db_path == cfg._PROJECT_ROOT / "data" / "dev.duckdb"
    monkeypatch.delenv("HART_DATA_DIR")
    monkeypatch.delenv("HART_DB_PATH")
    cfg.get_config(reload=True)
