"""Tests: hart server — storage guard, pipeline, jobs, scheduler, auth, MCP."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
import time
from pathlib import Path
from typing import Any

import pytest

from hart.analytics.phase import (
    comeback_end_for,
    detect_layoffs,
    find_longest_layoff,
    find_longest_missing_run,
)
from hart.config import ServerSettings, get_config
from hart.ingestion.sync_manager import SyncResult
from hart.server import state
from hart.server.jobs.handlers import apply_garmin_state
from hart.server.jobs.pipeline import persist_recovery_scores, run_sync_pipeline
from hart.server.jobs.runner import JobFailed, JobRunner
from hart.server.jobs.scheduler import Scheduler, garmin_blocked
from hart.server.seed import seed_all
from hart.storage.database import Database
from hart.storage.queries import run_analytics_query
from tests.conftest import write_seed

D = datetime.date


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.duckdb").connect()
    yield database
    database.close()


def _add_activity(db: Database, act_id: str, day: D, sport: str = "bike") -> None:
    db.execute(
        "INSERT INTO activities (activity_id, source, sport_type, start_time, elapsed_seconds) "
        "VALUES (?, 'test', ?, ?, 3600)",
        [act_id, sport, datetime.datetime.combine(day, datetime.time(7))],
    )


# ---------------------------------------------------------------------------
# run_sql_query guard
# ---------------------------------------------------------------------------


class TestSqlGuard:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM read_text('/etc/hosts')",
            "SELECT count(*) FROM glob('/etc/*')",
            "SELECT * FROM '/etc/passwd'",
            "SELECT * FROM 'data.csv'",
            "SELECT * FROM read_csv_auto('x.csv')",
            "SELECT 1; SELECT 2",
            "DELETE FROM activities",
            "WITH x AS (SELECT * FROM read_parquet('a.parquet')) SELECT * FROM x",
            "SELECT * FROM activities WHERE activity_id IN (SELECT * FROM glob('/tmp/*'))",
        ],
    )
    def test_rejects(self, db: Database, sql: str) -> None:
        with pytest.raises(ValueError):
            run_analytics_query(db, sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT count(*) AS n FROM activities",
            "WITH w AS (SELECT sport_type FROM activities) SELECT sport_type, count(*) FROM w GROUP BY 1",
            "SELECT a.activity_id FROM activities a LEFT JOIN activity_metrics m USING (activity_id)",
            "SELECT * FROM range(3)",
        ],
    )
    def test_allows(self, db: Database, sql: str) -> None:
        run_analytics_query(db, sql)


# ---------------------------------------------------------------------------
# Layoff detection
# ---------------------------------------------------------------------------


class TestLayoffs:
    def test_longest_gap(self) -> None:
        dates = [D(2026, 5, 1), D(2026, 5, 3), D(2026, 6, 10), D(2026, 9, 5), D(2026, 9, 7)]
        layoff = find_longest_layoff(dates)
        assert layoff is not None
        assert layoff.last_activity_date == D(2026, 6, 10)
        assert layoff.return_date == D(2026, 9, 5)
        assert layoff.gap_days == 86

    def test_no_gap_long_enough(self) -> None:
        assert find_longest_layoff([D(2026, 1, 1), D(2026, 1, 15)]) is None

    def test_missing_run(self) -> None:
        present = [D(2026, 6, 1) + datetime.timedelta(days=i) for i in range(10)]
        present += [D(2026, 9, 1) + datetime.timedelta(days=i) for i in range(10)]
        run = find_longest_missing_run(present, D(2026, 6, 1), D(2026, 9, 10))
        assert run == (D(2026, 6, 11), D(2026, 8, 31))

    def test_comeback_end_is_sunday(self) -> None:
        end = comeback_end_for(D(2026, 9, 3))  # Thursday
        assert end == D(2026, 11, 1)
        assert end.weekday() == 6

    def test_detect_ignores_walks(self, db: Database) -> None:
        _add_activity(db, "a1", D(2026, 6, 10))
        _add_activity(db, "w1", D(2026, 7, 20), sport="other")  # walk during the layoff
        _add_activity(db, "a2", D(2026, 9, 5))
        season = detect_layoffs(db, today=D(2026, 9, 26))
        assert season.layoff is not None
        assert season.layoff.return_date == D(2026, 9, 5)
        assert season.comeback_end == D(2026, 11, 1)


# ---------------------------------------------------------------------------
# Recovery persistence
# ---------------------------------------------------------------------------


class TestRecovery:
    def test_scores_only_dates_with_data(self, db: Database) -> None:
        db.execute(
            "INSERT INTO sleep_records (date, total_sleep_sec, sleep_score) VALUES (?, ?, ?)",
            [D(2026, 9, 20), 27000, 80],
        )
        written = persist_recovery_scores(db, [D(2026, 9, 19), D(2026, 9, 20)])
        assert written == 1
        rows = db.fetchall("SELECT date FROM daily_recovery")
        assert rows == [(D(2026, 9, 20),)]

    def test_null_health_values_are_not_nan(self, db: Database) -> None:
        # body_battery_start NULL must score as "missing", not as NaN.
        db.execute("INSERT INTO daily_health (date, resting_hr) VALUES (?, ?)", [D(2026, 9, 20), 50])
        persist_recovery_scores(db, [D(2026, 9, 20)])
        score = db.fetchone("SELECT recovery_score FROM daily_recovery")[0]
        assert score == score  # not NaN


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class FakeSyncManager:
    health_error: Exception | None = None
    new_activity_day: D | None = None

    def __init__(self, db: Database, config: Any) -> None:
        self.db = db

    def sync_garmin_health(self, days_back: int = 7) -> SyncResult:
        if self.health_error:
            raise self.health_error
        return SyncResult(new_health_days=days_back)

    def sync_garmin_activities(self, days_back: int = 7) -> SyncResult:
        if self.new_activity_day:
            _add_activity(self.db, "new_1", self.new_activity_day)
            return SyncResult(new_activities=1)
        return SyncResult()

    edited_strength: list[str] = []

    def refresh_recent_strength_sets(self, days: int, skip: set[str] | None = None) -> list[str]:
        return [a for a in self.edited_strength if a not in (skip or set())]

    edited_effort: list[str] = []

    def refresh_recent_effort(self, days: int, skip: set[str] | None = None) -> list[str]:
        return [a for a in self.edited_effort if a not in (skip or set())]


class GarminConnectAuthenticationError(Exception):
    pass


def _config_with_garmin() -> Any:
    base = get_config()
    return dataclasses.replace(base, garmin=dataclasses.replace(base.garmin, email="test@example.com"))


class TestPipeline:
    def test_full_run_reports_new_activities(self, db: Database) -> None:
        FakeSyncManager.health_error = None
        FakeSyncManager.new_activity_day = datetime.date.today()
        result = run_sync_pipeline(db, _config_with_garmin(), sync_manager_factory=FakeSyncManager)
        assert result["new_activity_ids"] == ["new_1"]
        assert result["error_code"] is None
        for name in ("training_load", "views", "anomalies", "sync_state", "strength_edits", "effort_edits"):
            assert result["steps"][name]["ok"], result["steps"][name]
        json.dumps(result, default=str)  # stored in jobs.result

    def test_auth_error_skips_activities_but_runs_derived_steps(self, db: Database) -> None:
        FakeSyncManager.health_error = GarminConnectAuthenticationError("expired")
        FakeSyncManager.new_activity_day = None
        result = run_sync_pipeline(db, _config_with_garmin(), sync_manager_factory=FakeSyncManager)
        assert result["error_code"] == "garmin_auth"
        assert result["partial"] is True
        assert "garmin_activities" not in result["steps"]
        assert result["steps"]["training_load"]["ok"]

    def test_garmin_state_pause_and_clear(self, db: Database) -> None:
        apply_garmin_state(db, "garmin_auth")
        assert garmin_blocked(db, datetime.datetime.now(tz=datetime.UTC)) == "garmin_auth"
        apply_garmin_state(db, None)
        assert garmin_blocked(db, datetime.datetime.now(tz=datetime.UTC)) is None

    def test_rate_limit_backoff(self, db: Database) -> None:
        apply_garmin_state(db, "garmin_rate_limited")
        now = datetime.datetime.now(tz=datetime.UTC)
        assert garmin_blocked(db, now) == "garmin_rate_limited"
        assert garmin_blocked(db, now + datetime.timedelta(hours=2)) is None


# ---------------------------------------------------------------------------
# Job runner and scheduler
# ---------------------------------------------------------------------------


def _wait_for(runner: JobRunner, job_id: int, timeout: float = 5) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = runner.get_job(job_id)
        if job and job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


class TestRunner:
    def test_runs_dedupes_and_records_failures(self, db: Database) -> None:
        def ok(cur: Database, payload: dict[str, Any]) -> dict[str, Any]:
            time.sleep(0.1)
            return {"echo": payload}

        def fail(cur: Database, payload: dict[str, Any]) -> dict[str, Any]:
            raise JobFailed("nope", {"partial": True})

        async def scenario() -> None:
            runner = JobRunner(db, {"sync": ok, "grade": fail})
            await runner.start()
            first = runner.enqueue("sync", {"n": 1}, dedupe_key="sync")
            second = runner.enqueue("sync", {"n": 2}, dedupe_key="sync")
            failing = runner.enqueue("grade", {})
            assert first["status"] == "queued"
            assert second["status"] in ("already_queued", "already_running")
            assert second["job_id"] == first["job_id"]
            assert second["message"].startswith(f"sync #{first['job_id']} is already ")
            assert len(runner.list_jobs(job_type="sync")) == 1  # no row for the duplicate
            done = await asyncio.to_thread(_wait_for, runner, first["job_id"])
            assert done["status"] == "ok" and done["result"] == {"echo": {"n": 1}}
            failed = await asyncio.to_thread(_wait_for, runner, failing["job_id"])
            assert failed["status"] == "error" and failed["error"] == "nope"
            assert failed["result"] == {"partial": True}
            await runner.stop()

        asyncio.run(scenario())

    def test_restart_marks_running_jobs_interrupted(self, db: Database) -> None:
        db.execute("INSERT INTO jobs (type, status, trigger) VALUES ('sync', 'running', 'manual')")
        db.execute("INSERT INTO jobs (type, status, trigger) VALUES ('sync', 'skipped', 'schedule')")

        async def scenario() -> None:
            runner = JobRunner(db, {"sync": lambda c, p: {}})
            await runner.start()
            jobs = runner.list_jobs()
            assert len(jobs) == 1  # legacy 'skipped' row removed
            assert jobs[0]["status"] == "error" and jobs[0]["error"] == "interrupted by restart"
            await runner.stop()

        asyncio.run(scenario())


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def enqueue(self, job_type: str, payload: Any = None, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(job_type)
        return {"job_id": len(self.calls), "status": "queued"}


class TestScheduler:
    def _at(self, hour: int, minute: int) -> datetime.datetime:
        from zoneinfo import ZoneInfo

        local = datetime.datetime(2026, 9, 26, hour, minute, tzinfo=ZoneInfo("Europe/Warsaw"))
        return local.astimezone(datetime.UTC)

    def test_morning_watch_and_hourly(self, db: Database) -> None:
        runner = RecordingRunner()
        sched = Scheduler(db, runner, "Europe/Warsaw")  # type: ignore[arg-type]
        sched.tick(self._at(5, 31))
        assert runner.calls == ["sync_light"]
        sched.tick(self._at(5, 40))  # same 15-min slot
        assert runner.calls == ["sync_light"]
        sched.tick(self._at(6, 1))  # new slot + first hourly sync
        assert runner.calls == ["sync_light", "sync_light", "sync"]

    def test_morning_watch_stops_once_sleep_exists(self, db: Database) -> None:
        db.execute("INSERT INTO sleep_records (date, total_sleep_sec) VALUES (?, 27000)", [D(2026, 9, 26)])
        runner = RecordingRunner()
        Scheduler(db, runner, "Europe/Warsaw").tick(self._at(5, 45))  # type: ignore[arg-type]
        assert runner.calls == []

    def test_paused_blocks_scheduled_syncs(self, db: Database) -> None:
        state.set_setting(db, state.GARMIN_AUTH_PAUSED, True)
        runner = RecordingRunner()
        Scheduler(db, runner, "Europe/Warsaw").tick(self._at(7, 0))  # type: ignore[arg-type]
        assert runner.calls == []


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def test_seed_is_idempotent(db: Database, tmp_path: Path) -> None:
    _add_activity(db, "a1", D(2026, 6, 10))
    _add_activity(db, "a2", D(2026, 9, 5))
    (tmp_path / "athlete_notes.json").write_text(
        json.dumps(
            [
                {"category": "goal", "title": "IM", "body": "race", "rules": None},
            ]
        )
    )
    write_seed(tmp_path)
    (tmp_path / "annotations.json").write_text(
        json.dumps([{"kind": "event", "label": "Camp", "start_date": "2026-09-19", "end_date": "2026-09-21"}])
    )
    first = seed_all(db, tmp_path)
    second = seed_all(db, tmp_path)
    assert first["races"] == 1 and first["notes"] == 1
    assert second == {**second, "races": 0, "notes": 0}
    assert db.fetchone("SELECT status FROM athlete_notes")[0] == "proposed"
    kinds = {r[0] for r in db.fetchall("SELECT kind FROM annotations")}
    assert {"other", "event"} <= kinds  # the detected training break + the seeded event


# ---------------------------------------------------------------------------
# App: auth, CSRF, API, MCP
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from hart.server.app import create_app

    config = dataclasses.replace(
        get_config(),
        db_path=tmp_path / "app.duckdb",
        garmin_token_path=tmp_path / "tokens",
        server=ServerSettings(env="production", seed_dir=write_seed(tmp_path / "seed")),
    )
    handlers = {"sync": lambda db, p: {"fake": True}, "sync_light": lambda db, p: {"fake": True}}
    app = create_app(config, run_scheduler=False, handlers=handlers)
    with TestClient(app, base_url="https://hart.example.ts.net") as c:
        yield c


USER = {"Tailscale-User-Login": "athlete@example.com"}
WRITE = {**USER, "X-Requested-With": "hart"}


class TestApp:
    def test_healthz_is_open(self, client) -> None:
        resp = client.get("/healthz")
        assert resp.status_code == 200 and resp.json()["runner_alive"] is True

    def test_requires_tailscale_identity(self, client) -> None:
        assert client.get("/api/system").status_code == 403
        assert client.get("/api/system", headers=USER).status_code == 200

    def test_csrf_header_required_for_writes(self, client) -> None:
        assert client.post("/api/sync", json={}, headers=USER).status_code == 403
        resp = client.post("/api/sync", json={}, headers=WRITE)
        assert resp.status_code == 200 and resp.json()["status"] == "queued"

    def test_foreign_origin_rejected(self, client) -> None:
        resp = client.get("/api/system", headers={**USER, "Origin": "https://evil.example"})
        assert resp.status_code == 403
        resp = client.get("/api/system", headers={**USER, "Origin": "https://hart.example.ts.net"})
        assert resp.status_code == 200

    def test_sync_job_runs(self, client) -> None:
        job = client.post("/api/sync", json={"full": True}, headers=WRITE).json()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = client.get(f"/api/jobs/{job['job_id']}", headers=USER).json()
            if status["status"] == "ok":
                break
            time.sleep(0.05)
        assert status["status"] == "ok" and status["result"] == {"fake": True}

    def test_system_page_renders(self, client) -> None:
        resp = client.get("/system", headers=USER)
        assert resp.status_code == 200 and "Sync now" in resp.text

    def test_garmin_token_upload(self, client, tmp_path: Path) -> None:
        bad = client.post("/api/garmin/tokens", json={"files": {"../x.json": "{}"}}, headers=WRITE)
        assert bad.status_code == 400
        ok = client.post("/api/garmin/tokens", json={"files": {"garmin_tokens.json": "{}"}}, headers=WRITE)
        assert ok.status_code == 200
        assert (tmp_path / "tokens" / "garmin_tokens.json").read_text() == "{}"

    def _mcp(self, client, method: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        return client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers={**headers, "Accept": "application/json, text/event-stream"},
        )

    def test_mcp_requires_identity_or_internal_token(self, client) -> None:
        assert self._mcp(client, "tools/list", {}, {}).status_code == 403
        token = client.app.state.internal_token
        resp = self._mcp(client, "tools/list", {}, {"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        names = {t["name"] for t in resp.json()["result"]["tools"]}
        assert {"get_activities", "get_job_status", "run_sql_query"} <= names

    def test_mcp_tool_call_uses_server_db_and_jobs(self, client) -> None:
        resp = self._mcp(
            client,
            "tools/call",
            {"name": "run_sql_query", "arguments": {"sql": "SELECT name FROM races"}},
            USER,
        )
        payload = json.loads(resp.json()["result"]["content"][0]["text"])
        assert payload["rows"] == [{"name": "Example Ironman"}]

        resp = self._mcp(client, "tools/call", {"name": "sync_all", "arguments": {}}, USER)
        job = json.loads(resp.json()["result"]["content"][0]["text"])
        assert job["status"] in ("queued", "already_queued", "already_running")
        resp = self._mcp(client, "tools/call", {"name": "get_job_status", "arguments": {"job_id": job["job_id"]}}, USER)
        assert "status" in json.loads(resp.json()["result"]["content"][0]["text"])


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------


class TestBackup:
    def test_requires_marker(self, db: Database, tmp_path: Path) -> None:
        from hart.server.jobs.backup import run_backup

        with pytest.raises(JobFailed, match="not available"):
            run_backup(db, tmp_path, tmp_path / "tokens")

    def test_export_and_prune(self, db: Database, tmp_path: Path) -> None:
        from hart.server.jobs.backup import MARKER, prune_backups, run_backup

        backups = tmp_path / "backups"
        backups.mkdir()
        (backups / MARKER).touch()
        tokens = tmp_path / "tokens"
        tokens.mkdir()
        (tokens / "garmin_tokens.json").write_text("{}")
        _add_activity(db, "a1", D(2026, 9, 1))

        out = run_backup(db, backups, tokens, today=D(2026, 9, 26))
        final = backups / "2026-09-26"
        assert Path(out["path"]) == final and (final / "db" / "schema.sql").is_file()
        assert (final / "garmin_tokens" / "garmin_tokens.json").is_file()
        assert not (backups / "2026-09-26.partial").exists()

        # Restorable: IMPORT DATABASE into a fresh file.
        import duckdb

        con = duckdb.connect(str(tmp_path / "restored2.duckdb"))
        con.execute(f"IMPORT DATABASE '{final / 'db'}'")
        assert con.execute("SELECT count(*) FROM activities").fetchone()[0] == 1
        con.close()

        for day in ("2026-09-01", "2026-09-06", "2026-07-26", "2026-09-20"):
            (backups / day).mkdir()
        (backups / "2026-09-25.partial").mkdir()
        removed = prune_backups(backups, D(2026, 9, 26))
        # 09-01 (Tue) is older than 14 days → removed; 09-06 (Sun) kept as weekly;
        # 07-26 (Sun) older than 8 weeks → removed; 09-20 within 14 days → kept.
        assert set(removed) == {"2026-09-01", "2026-07-26", "2026-09-25.partial"}

    def test_backup_scheduled_once_in_window(self, db: Database) -> None:
        runner = RecordingRunner()
        sched = Scheduler(db, runner, "Europe/Warsaw")  # type: ignore[arg-type]
        at = TestScheduler()._at
        sched.tick(at(2, 59))
        sched.tick(at(3, 1))
        sched.tick(at(3, 30))
        sched.tick(at(14, 0))
        assert runner.calls.count("backup") == 1


def test_pipeline_flags_missing_garmin_credentials(db: Database) -> None:
    config = dataclasses.replace(get_config(), garmin=dataclasses.replace(get_config().garmin, email=""))
    FakeSyncManager.health_error = None
    FakeSyncManager.new_activity_day = None
    result = run_sync_pipeline(db, config, sync_manager_factory=FakeSyncManager)
    assert result["error_code"] == "garmin_not_configured"
    assert "garmin_activities" not in result["steps"]
    assert result["steps"]["training_load"]["ok"]


def test_job_details_summaries() -> None:
    from hart.server.app import job_details

    sync = {
        "type": "sync",
        "status": "ok",
        "error": None,
        "result": {
            "steps": {
                "garmin_health": {"out": {"days": 2, "errors": 0, "from": "2026-09-25", "to": "2026-09-26"}},
                "garmin_activities": {"out": {"new": 1, "errors": 0}},
                "recovery": {"out": {"written": 2}},
                "anomalies": {"out": {"detected": 3, "inserted": 0}},
            },
            "errors": [],
        },
    }
    assert job_details(sync) == (
        "health refreshed for 25–26 Sep · 1 new activity · recovery recalculated for 2 days · no new anomalies"
    )
    backup = {
        "type": "backup",
        "status": "ok",
        "error": None,
        "result": {"bytes": 44_040_192, "path": "/backups/2026-09-26", "pruned": []},
    }
    assert job_details(backup) == "42 MB → /backups/2026-09-26"


def test_seed_refreshes_only_unapproved_seed_notes(db: Database, tmp_path: Path) -> None:
    from hart.server.seed import seed_notes

    seed = tmp_path / "athlete_notes.json"
    seed.write_text(
        json.dumps(
            [
                {"category": "injury", "title": "Shoulder", "body": "v1", "rules": None},
                {"category": "goal", "title": "Race", "body": "v1", "rules": None},
            ]
        )
    )
    assert seed_notes(db, tmp_path) == 2
    db.execute("UPDATE athlete_notes SET status = 'active' WHERE title = 'Race'")

    seed.write_text(
        json.dumps(
            [
                {
                    "category": "injury",
                    "title": "Shoulder",
                    "body": "v2",
                    "rules": {"allowed_weekdays": {"swim": ["thu"]}},
                },
                {"category": "goal", "title": "Race", "body": "v2", "rules": None},
            ]
        )
    )
    assert seed_notes(db, tmp_path) == 1
    rows = dict(db.fetchall("SELECT title, body FROM athlete_notes"))
    assert rows == {"Shoulder": "v2", "Race": "v1"}  # approved note untouched
    assert seed_notes(db, tmp_path) == 0  # idempotent


def test_health_upsert_keeps_values_when_garmin_returns_nothing(db: Database) -> None:
    from hart.models.health import HealthDay, SleepRecord
    from hart.storage.writers import upsert_health_day, upsert_sleep_record

    day = D(2026, 9, 25)
    upsert_health_day(db, HealthDay(date=day, resting_hr=50, training_readiness=70, steps=8000))
    # Re-fetch where the readiness call failed (None) but steps updated.
    upsert_health_day(db, HealthDay(date=day, resting_hr=51, training_readiness=None, steps=9000))
    assert db.fetchone("SELECT resting_hr, training_readiness, steps FROM daily_health WHERE date = ?", [day]) == (
        51,
        70,
        9000,
    )

    upsert_sleep_record(db, SleepRecord(date=day, total_sleep_sec=27000, sleep_score=80))
    upsert_sleep_record(db, SleepRecord(date=day, total_sleep_sec=None, sleep_score=82))
    assert db.fetchone("SELECT total_sleep_sec, sleep_score FROM sleep_records WHERE date = ?", [day]) == (27000, 82)
