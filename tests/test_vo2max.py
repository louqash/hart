"""VO2max ingestion from Garmin's max-metrics service (daily sync + backfill)."""

from __future__ import annotations

import dataclasses
import datetime
import time
from pathlib import Path
from typing import Any

import pytest

from hart.config import ServerSettings, get_config
from hart.ingestion.sync_manager import SyncManager, _parse_max_metrics
from hart.models.health import HealthDay
from hart.storage.database import Database
from hart.storage.writers import upsert_health_day

D = datetime.date


def _entry(day: str, run: float | None = None, cycle: float | None = None) -> dict[str, Any]:
    """One max-metrics entry shaped like Garmin's response."""

    def block(value: float | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return {"calendarDate": day, "vo2MaxPreciseValue": value, "vo2MaxValue": round(value)}

    return {
        "userId": 1,
        "generic": block(run),
        "cycling": block(cycle),
        "heatAltitudeAcclimation": {"calendarDate": day, "vo2MaxPreciseValue": None},
    }


class FakeGarmin:
    """Answers the calls SyncManager makes; VO2max comes only from max-metrics."""

    garmin_connect_metrics_url = "/metrics-service/metrics/maxmet/daily"

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = entries
        self.range_calls: list[str] = []

    def _in_range(self, start: str, end: str) -> list[dict[str, Any]]:
        def day(e: dict[str, Any]) -> str:
            return (e.get("generic") or e.get("cycling") or {}).get("calendarDate", "")

        return [e for e in self.entries if start <= day(e) <= end]

    def get_stats(self, date_str: str) -> dict[str, Any]:
        # The real daily summary has no VO2max keys.
        return {"restingHeartRate": 48, "totalSteps": 9000}

    def get_max_metrics(self, date_str: str) -> list[dict[str, Any]]:
        return self._in_range(date_str, date_str)

    def connectapi(self, url: str) -> list[dict[str, Any]]:
        self.range_calls.append(url)
        start, end = url.rsplit("/", 2)[-2:]
        return self._in_range(start, end)

    def get_training_readiness(self, date_str: str) -> None:
        return None

    def get_stress_data(self, date_str: str) -> None:
        return None

    def get_heart_rates(self, date_str: str) -> None:
        return None

    def get_sleep_data(self, date_str: str) -> None:
        return None

    def get_hrv_data(self, date_str: str) -> None:
        return None


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.duckdb").connect()
    yield database
    database.close()


def _config() -> Any:
    base = get_config()
    return dataclasses.replace(base, garmin=dataclasses.replace(base.garmin, email="test@example.com"))


def _manager(db: Database, garmin: FakeGarmin) -> SyncManager:
    manager = SyncManager(db, _config())
    manager._garmin = garmin
    return manager


def _vo2(db: Database, day: D) -> tuple[Any, Any]:
    return db.fetchone("SELECT vo2max_run, vo2max_cycle FROM daily_health WHERE date = ?", [day])


def test_parse_prefers_precise_and_merges_blocks() -> None:
    parsed = _parse_max_metrics(
        [
            _entry("2026-06-02", run=51.4),
            _entry("2026-06-02", cycle=54.5),
            {"generic": {"calendarDate": "2026-06-03", "vo2MaxPreciseValue": None, "vo2MaxValue": 52}},
            {"generic": None, "cycling": None},
        ]
    )
    assert parsed == {D(2026, 6, 2): (51.4, 54.5), D(2026, 6, 3): (52.0, None)}
    assert _parse_max_metrics(None) == {}
    assert _parse_max_metrics({"error": "x"}) == {}


def test_daily_sync_reads_vo2max_from_max_metrics(db: Database) -> None:
    day = D(2026, 6, 2)
    manager = _manager(db, FakeGarmin([_entry("2026-06-02", run=51.4, cycle=54.5)]))
    manager._sync_health_day(manager._garmin, day, day.isoformat())
    assert _vo2(db, day) == (51.4, 54.5)


def test_day_without_estimate_keeps_stored_vo2max(db: Database) -> None:
    day = D(2026, 6, 2)
    upsert_health_day(db, HealthDay(date=day, vo2max_run=50.0, vo2max_cycle=53.0))
    manager = _manager(db, FakeGarmin([]))  # no estimate that day → NULLs from the sync
    manager._sync_health_day(manager._garmin, day, day.isoformat())
    assert _vo2(db, day) == (50.0, 53.0)
    assert db.fetchone("SELECT resting_hr FROM daily_health WHERE date = ?", [day])[0] == 48


def test_backfill_updates_existing_days_only(db: Database) -> None:
    upsert_health_day(db, HealthDay(date=D(2025, 1, 10), resting_hr=50))
    upsert_health_day(db, HealthDay(date=D(2026, 3, 1), resting_hr=49, vo2max_run=49.0))
    upsert_health_day(db, HealthDay(date=D(2026, 6, 2), resting_hr=47))
    garmin = FakeGarmin(
        [
            _entry("2025-01-10", run=48.2),
            _entry("2025-07-01", cycle=52.0),  # no daily_health row → skipped
            _entry("2026-03-01", cycle=53.1),  # run already stored, must survive
            _entry("2026-06-02", run=51.4, cycle=54.5),
        ]
    )

    result = _manager(db, garmin).backfill_vo2max(end=D(2026, 9, 26))

    assert result == {
        "from": "2025-01-10",
        "to": "2026-09-26",
        "estimates": 4,
        "updated": 3,
        "no_health_row": 1,
    }
    assert len(garmin.range_calls) == 2  # yearly chunks, not one call per day
    assert _vo2(db, D(2025, 1, 10)) == (48.2, None)
    assert _vo2(db, D(2026, 3, 1)) == (49.0, 53.1)
    assert _vo2(db, D(2026, 6, 2)) == (51.4, 54.5)
    assert db.fetchone("SELECT resting_hr FROM daily_health WHERE date = ?", [D(2026, 6, 2)])[0] == 47
    assert db.fetchone("SELECT COUNT(*) FROM daily_health")[0] == 3


def test_backfill_job_via_api(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from hart.server.app import create_app, job_details

    seen: list[dict[str, Any]] = []

    def fake_backfill(db: Database, payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        return {"from": "2026-01-01", "to": "2026-09-26", "estimates": 5, "updated": 4, "no_health_row": 1}

    config = dataclasses.replace(
        get_config(),
        db_path=tmp_path / "app.duckdb",
        garmin_token_path=tmp_path / "tokens",
        server=ServerSettings(env="production", seed_dir=tmp_path / "seed"),
    )
    handlers = {"sync": lambda db, p: {}, "sync_light": lambda db, p: {}, "vo2max_backfill": fake_backfill}
    app = create_app(config, run_scheduler=False, handlers=handlers)
    headers = {"Tailscale-User-Login": "athlete@example.com", "X-Requested-With": "hart"}
    with TestClient(app, base_url="https://hart.example.ts.net") as client:
        job = client.post("/api/backfill/vo2max", json={"start": "2026-01-01"}, headers=headers).json()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = client.get(f"/api/jobs/{job['job_id']}", headers=headers).json()
            if status["status"] not in ("queued", "running"):
                break
            time.sleep(0.05)

    assert status["status"] == "ok"
    assert seen == [{"start": "2026-01-01"}]
    assert job_details(status) == (
        "VO2max set on 4 days (2026-01-01 → 2026-09-26) · 1 estimate(s) skipped (no health row)"
    )
