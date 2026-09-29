"""Steps: readiness context and the Vital Signs chart data."""

from __future__ import annotations

import datetime
from pathlib import Path

from hart.analytics.steps import step_context
from hart.server.data import health_series, steps_context
from hart.storage.database import Database


def test_step_context_flags() -> None:
    usual = [8000, 9000, 10000, 9500, 8500, 9000, 10500]
    assert step_context(None, usual) is None
    assert step_context(9000, usual[:3])["usual"] is None  # not enough history
    normal = step_context(9800, usual)
    assert normal["usual"] == 9000 and normal["flag"] is None
    assert step_context(24000, usual)["flag"] == "high"
    assert step_context(14000, [5000] * 7)["flag"] is None  # a big ratio, but not a big day
    assert step_context(1500, usual)["flag"] == "low"


def test_steps_in_readiness_and_series(tmp_path: Path) -> None:
    db = Database(tmp_path / "s.duckdb").connect()
    today = datetime.date(2026, 9, 29)
    for i in range(1, 20):
        day = today - datetime.timedelta(days=i)
        db.execute("INSERT INTO daily_health (date, steps) VALUES (?, ?)", [day, 26000 if i == 1 else 9000])
    ctx = steps_context(db, today)
    assert ctx["date"] == today - datetime.timedelta(days=1) and ctx["flag"] == "high" and ctx["usual"] == 9000
    series = health_series(db, today, "90d")["steps"]
    assert series[-1]["value"] == 26000 and series[-1]["avg7"] == round((26000 + 6 * 9000) / 7)
    db.close()
