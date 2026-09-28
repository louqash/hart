"""Tests: grading features, scoring, citations, grade jobs and API."""

from __future__ import annotations

import dataclasses
import datetime
import time
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import RateLimitEvent, RateLimitInfo

from hart.analytics.grading_features import build_features
from hart.config import ServerSettings, get_config
from hart.server.grading import Citation, Scores, overall, verify_citations
from hart.server.state import DEFAULT_THRESHOLDS
from hart.storage.database import Database
from tests.test_chat import FakeClient, _result

NOW = datetime.datetime.now().replace(microsecond=0)


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    FakeClient.instances.clear()
    FakeClient.scripts.clear()


def _activity(
    db: Database,
    act_id: str,
    start: datetime.datetime,
    sport: str = "bike",
    sub: str = "indoor_cycling",
    secs: int = 3600,
    hr: float = 130,
    power: float | None = 150,
) -> None:
    db.execute(
        "INSERT INTO activities (activity_id, source, sport_type, sub_type, name, start_time, elapsed_seconds, "
        "moving_seconds, avg_hr, avg_power, normalized_power) VALUES (?, 'test', ?, ?, 'Ride', ?, ?, ?, ?, ?, ?)",
        [act_id, sport, sub, start, secs, secs, hr, power, power],
    )
    db.execute(
        "INSERT INTO activity_metrics (activity_id, sport_type, date, tss, hr_zone_seconds, efficiency_factor) "
        'VALUES (?, ?, ?, 40, \'{"Z1": 600, "Z2": 2800, "Z3": 200}\', ?)',
        [act_id, sport, start.date(), (power or 0) / hr if power else None],
    )


def _streams(db: Database, act_id: str, n: int = 1800, drift: float = 0.05) -> None:
    rows = [(act_id, i, int(125 + 10 * drift * i / n * 20), 150) for i in range(n)]
    db.executemany(
        "INSERT INTO activity_streams (activity_id, timestamp_sec, heart_rate, power) VALUES (?, ?, ?, ?)", rows
    )


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "g.duckdb").connect()
    yield database
    database.close()


# ---------------------------------------------------------------------------
# Scoring and citations
# ---------------------------------------------------------------------------


def test_overall_and_letters() -> None:
    assert overall(Scores(execution=5, response=5, context_fit=5)) == (5.0, "A")
    assert overall(Scores(execution=4, response=4, context_fit=3))[1] == "B"  # 3.75
    assert overall(Scores(execution=3, response=None, context_fit=3)) == (3.0, "C")  # renormalised
    assert overall(Scores(execution=None, response=None, context_fit=2)) == (2.0, "E")
    assert overall(Scores(execution=2, response=3, context_fit=2))[1] == "D"  # 2.3


def test_verify_citations() -> None:
    sources = ['{"decoupling_pct": 4.23, "avg_hr": 131, "load": 26}']
    out = verify_citations(
        [
            Citation(label="decoupling", value=4.2, unit="%", source="features"),
            Citation(label="avg HR", value=131, source="features"),
            Citation(label="made up", value=77.7, source="features"),
            Citation(label="load", value="26", source="features"),
        ],
        sources,
    )
    assert [c["verified"] for c in out] == [True, True, False, True]


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------


def test_features_for_indoor_ride(db: Database) -> None:
    for i in range(1, 5):
        _activity(db, f"old{i}", NOW - datetime.timedelta(days=7 * i), hr=135)
    _activity(db, "ride", NOW - datetime.timedelta(hours=2), hr=130)
    _streams(db, "ride")
    f = build_features(db, "ride", DEFAULT_THRESHOLDS)
    assert f["gradable"] and f["basics"]["indoor"] is True
    assert f["intensity"]["hr_zone_pct"] == {"Z1": 17, "Z2": 78, "Z3": 6}
    dec = f["durability"]["stream_decoupling"]
    assert dec["basis"] == "power/HR" and dec["decoupling_pct"] > 0  # HR drifted up at constant power
    comp = f["comparison"]
    assert comp["count"] == 4 and comp["medians"]["avg_hr"] == 135 and comp["this_vs_median_pct"]["avg_hr"] == -3.7
    assert f["data_quality"]["single_sided_power"] is False  # not set in Settings
    from hart.server import settings

    settings.set_value(db, "power_single_sided", True)
    assert build_features(db, "ride", DEFAULT_THRESHOLDS)["data_quality"]["single_sided_power"] is True


def test_eligibility(db: Database) -> None:
    _activity(db, "walk", NOW, sport="other", sub="generic", power=None)
    _activity(db, "short", NOW, secs=300)
    assert build_features(db, "walk", DEFAULT_THRESHOLDS)["ungraded_reason"].startswith("Not a training session")
    assert build_features(db, "short", DEFAULT_THRESHOLDS)["ungraded_reason"] == "Shorter than 10 minutes"


# ---------------------------------------------------------------------------
# Grade jobs through the app
# ---------------------------------------------------------------------------

GRADE = {
    "session_type": "endurance",
    "scores": {"execution": 4, "response": 3, "context_fit": 5},
    "justifications": {
        "execution": "Steady Z2.",
        "response": "HR 3.7% below similar rides.",
        "context_fit": "Right for the comeback.",
    },
    "confidence": "high",
    "summary": "Controlled endurance ride; HR was 3.7% lower than similar rides.",
    "highlights": ["78% of the time in Z2"],
    "concerns": [],
    "citations": [
        {"label": "HR vs similar", "value": -3.7, "unit": "%", "source": "features.comparison"},
        {"label": "Z2 share", "value": 78, "unit": "%", "source": "features.intensity"},
    ],
}


@pytest.fixture
def client(tmp_path: Path):
    from fastapi.testclient import TestClient

    from hart.server.app import create_app

    db_path = tmp_path / "app.duckdb"
    seed = Database(db_path).connect()
    for i in range(1, 5):
        _activity(seed, f"old{i}", NOW - datetime.timedelta(days=7 * i), hr=135)
    _activity(seed, "ride", NOW - datetime.timedelta(hours=2), hr=130)
    _streams(seed, "ride")
    _activity(seed, "walk", NOW - datetime.timedelta(hours=5), sport="other", sub="generic", power=None)
    seed.close()
    config = dataclasses.replace(
        get_config(), db_path=db_path, server=ServerSettings(env="production", seed_dir=tmp_path / "seed")
    )
    app = create_app(config, run_scheduler=False, claude_client_factory=FakeClient)
    with TestClient(app, base_url="https://hart.example.ts.net") as c:
        yield c


H = {"Tailscale-User-Login": "me@example.com"}
W = {**H, "X-Requested-With": "hart"}


def _wait_job(client, job_id: int) -> dict[str, Any]:
    for _ in range(300):
        job = client.get(f"/api/jobs/{job_id}", headers=H).json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def _grade(client, activity_id: str, **body: Any) -> dict[str, Any]:
    job = client.post(f"/api/sessions/{activity_id}/grade", headers=W, json=body).json()
    return _wait_job(client, job["job_id"])


def test_grade_end_to_end(client) -> None:
    FakeClient.scripts.append([_result(structured_output=GRADE)])
    job = _grade(client, "ride")
    assert job["status"] == "ok" and job["result"]["letter"] == "B"  # 0.45·4 + 0.3·3 + 0.25·5 = 3.95
    g = client.get("/api/sessions/ride/grade", headers=H).json()
    assert g["letter"] == "B" and g["overall_score"] == 3.95 and g["confidence"] == "high"
    assert [c["verified"] for c in g["citations"]] == [True, True]
    assert g["features"]["justifications"]["execution"] == "Steady Z2."
    opts = FakeClient.instances[0].options
    assert opts.model == "claude-sonnet-5" and opts.output_format["type"] == "json_schema"
    assert "WebSearch" not in opts.tools  # grading never goes to the web
    # Chips and pages show it.
    listing = client.get("/api/sessions", headers=H).json()["items"]
    assert next(s for s in listing if s["activity_id"] == "ride")["grade"]["letter"] == "B"
    assert "Trail Report" in client.get("/sessions/ride", headers=H).text


def test_invalid_output_is_repaired_once(client) -> None:
    FakeClient.scripts.append([_result(structured_output={"session_type": "nonsense"})])
    FakeClient.scripts.append([_result(structured_output=GRADE)])
    assert _grade(client, "ride")["result"]["letter"] == "B"
    assert "failed validation" in FakeClient.instances[1].prompt


def test_unverified_citations_force_low_confidence(client) -> None:
    fake = {**GRADE, "citations": [{"label": "x", "value": 999.9, "source": "?"}, *GRADE["citations"][:1]]}
    FakeClient.scripts.append([_result(structured_output=fake)])
    _grade(client, "ride")
    assert client.get("/api/sessions/ride/grade", headers=H).json()["confidence"] == "low"


def test_walks_are_ungraded_without_claude(client) -> None:
    job = _grade(client, "walk")
    assert job["result"]["status"] == "ungraded" and FakeClient.instances == []
    FakeClient.scripts.append([_result(structured_output=GRADE)])
    assert _grade(client, "walk", force=True)["result"]["status"] == "graded"


def test_usage_limit_pauses_grading(client) -> None:
    FakeClient.scripts.append(
        [
            RateLimitEvent(
                rate_limit_info=RateLimitInfo(status="rejected", resets_at=int(time.time()) + 3600),
                uuid="u",
                session_id="s",
            ),
            _result(is_error=True, api_error_status=429),
        ]
    )
    job = _grade(client, "ride")
    assert job["status"] == "error" and "usage limit" in job["error"]
    job2 = _grade(client, "ride")  # paused: Claude isn't called again
    assert job2["status"] == "error" and "paused until" in job2["error"] and len(FakeClient.instances) == 1


def test_feedback_triggers_one_regrade(client) -> None:
    FakeClient.scripts.extend([[_result(structured_output=GRADE)] for _ in range(3)])
    _grade(client, "ride")
    fb = client.post("/api/sessions/ride/feedback", headers=W, json={"rpe": 4, "comment": "easy"}).json()
    assert fb["regrade"]["status"] == "queued"
    _wait_job(client, fb["regrade"]["job_id"])
    again = client.post("/api/sessions/ride/feedback", headers=W, json={"rpe": 5}).json()
    assert again["regrade"] is None  # only once
    versions = client.get("/api/sessions/ride", headers=H).json()["grade_versions"]
    assert versions == [2, 1]
    assert client.get("/api/sessions/ride/grade?version=2", headers=H).json()["features"]["feedback"]["rpe"] == 4


def test_sweep_finds_ungraded(client) -> None:
    from hart.server.jobs.scheduler import ungraded_recent

    with client.app.state.db.cursor() as cur:
        assert set(ungraded_recent(cur)) == {"ride", "walk"}
        cur.execute("INSERT INTO session_grades (activity_id, version, status) VALUES ('walk', 1, 'ungraded')")
        assert ungraded_recent(cur) == ["ride"]


def test_backfill_queues_ungraded(client) -> None:
    FakeClient.scripts.extend([[_result(structured_output=GRADE)] for _ in range(6)])
    out = client.post("/api/grades/backfill", headers=W, json={"days": 60}).json()
    assert out["activities"] == 6 and out["queued"] == 6


def test_session_compare(client) -> None:
    out = client.get("/api/sessions/ride/compare", headers=H).json()
    assert out["other"]["id"] == "old1" and len(out["candidates"]) == 4  # most recent similar first
    hr = next(r for r in out["rows"] if r["label"] == "Avg HR")
    assert (hr["this"], hr["other"], hr["delta_pct"]) == (130, 135, -3.7)
    picked = client.get("/api/sessions/ride/compare?other=old3", headers=H).json()
    assert picked["other"]["id"] == "old3"
    page = client.get("/sessions/ride?compare=old2", headers=H).text
    assert "Side by Side" in page and 'value="old2" selected' in page
    assert client.get("/api/sessions/walk/compare", headers=H).json()["other"] is None


def test_fitness_markers_with_power(client) -> None:
    markers = client.get("/api/fitness/markers", headers=H).json()
    power = next(m for m in markers if m["key"] == "power_300")
    assert power["value"] == 150 and "single-sided" not in power["note"]
    client.put("/api/settings/power_single_sided", headers=W, json={"value": True})
    power = next(m for m in client.get("/api/fitness/markers", headers=H).json() if m["key"] == "power_300")
    assert "single-sided" in power["note"]
    assert "single-sided meter" in client.get("/fitness", headers=H).text
