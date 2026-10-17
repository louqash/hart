"""Tests: planned vs done (analytics.plan_compare)."""

from __future__ import annotations

from typing import Any

from hart.analytics.plan_compare import compare_to_plan, zone_shares

EASY = {"Z1": 900, "Z2": 2500, "Z3": 200}  # 6% Z3+, no Z4
THRESHOLD = {"Z1": 600, "Z2": 1800, "Z3": 900, "Z4": 2100, "Z5": 0}  # 56% Z3+, 39% Z4+ of 90 min


def _cmp(
    sport: str = "bike",
    minutes: float | None = 60,
    intensity: str | None = "endurance",
    done: str = "bike",
    seconds: float | None = 3600,
    zones: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return compare_to_plan(
        planned_sport=sport,
        planned_minutes=minutes,
        planned_intensity=intensity,
        done_sport=done,
        done_seconds=seconds,
        hr_zone_seconds=EASY if zones is None else zones,
    )


def test_zone_shares() -> None:
    assert zone_shares(THRESHOLD) == {"z3_plus_pct": 55.6, "z4_plus_pct": 38.9, "z5_pct": 0.0}
    assert zone_shares({"Z1": 0, "Z2": None}) is None
    assert zone_shares(None) is None


def test_as_planned() -> None:
    c = _cmp()
    assert c["verdict"] == "as_planned" and c["deviations"] == [] and c["sport_match"]
    assert c["duration_ratio"] == 1.0 and c["intensity_vs_plan"] == "as_planned" and c["not_checked"] == []
    assert c["summary"].startswith("Planned bike 60 min endurance; did bike 60 min (100% of the planned time")
    assert c["summary"].endswith(": as planned.")
    # Within the duration tolerance either way.
    assert _cmp(seconds=50 * 60)["verdict"] == "as_planned" and _cmp(seconds=74 * 60)["verdict"] == "as_planned"


def test_easy_spin_instead_of_intervals_is_easier_and_shorter() -> None:
    c = _cmp(minutes=90, intensity="threshold", seconds=30 * 60)
    assert c["verdict"] == "easier" and c["deviations"] == ["easier", "shorter"]
    assert c["duration_ratio"] == 0.33 and c["done"]["minutes"] == 30 and c["done"]["z4_plus_pct"] == 0.0
    assert "33% of the planned time" in c["summary"] and c["summary"].endswith(": easier and shorter.")


def test_shorter_and_longer() -> None:
    assert _cmp(seconds=40 * 60)["deviations"] == ["shorter"]
    assert _cmp(seconds=80 * 60)["deviations"] == ["longer"]


def test_harder_than_planned() -> None:
    assert _cmp(zones=THRESHOLD)["verdict"] == "harder"  # endurance planned, threshold ridden
    assert _cmp(intensity="recovery", zones={"Z1": 3000, "Z3": 500})["verdict"] == "harder"  # 14% Z3+
    assert _cmp(intensity="threshold", zones={"Z2": 1000, "Z4": 1000, "Z5": 1000})["verdict"] == "harder"  # Z5 33%
    assert _cmp(intensity="threshold", zones=THRESHOLD)["verdict"] == "as_planned"


def test_easier_than_planned() -> None:
    assert _cmp(intensity="tempo")["verdict"] == "easier"  # 6% Z3+
    assert _cmp(intensity="vo2", zones={"Z2": 3000, "Z3": 400, "Z4": 200})["verdict"] == "easier"  # 6% Z4+
    assert _cmp(intensity="vo2", zones={"Z2": 2400, "Z4": 600, "Z5": 600})["verdict"] == "as_planned"


def test_different_sport() -> None:
    c = _cmp(sport="run", minutes=45, done="bike")
    assert c["verdict"] == "different_sport" and c["deviations"] == ["different_sport", "longer"]
    assert c["intensity_vs_plan"] is None  # zones of another sport say nothing about the planned intensity
    assert c["summary"].startswith("Planned run 45 min endurance; did bike 60 min")


def test_what_cant_be_checked() -> None:
    c = _cmp(minutes=None, intensity=None, zones={})
    assert c["verdict"] == "as_planned" and c["duration_ratio"] is None
    assert c["not_checked"] == ["duration: no planned duration", "intensity: no planned intensity"]
    assert _cmp(zones={})["not_checked"] == ["intensity: no heart-rate zones"]
    strength = _cmp(sport="strength", minutes=45, intensity="strength", done="strength", seconds=45 * 60)
    assert strength["verdict"] == "as_planned" and strength["not_checked"][0].startswith("intensity: planned")
    assert _cmp(seconds=None)["not_checked"] == ["duration: no recorded duration"]
