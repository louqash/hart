"""Planned vs done: how a session compares with the plan row it was linked to.

A plan row counts as done as soon as an activity is linked to it (by date and
sport, or by hand), so a 30-minute easy spin can "complete" a planned
90-minute interval ride.  This module says how the two differ, in facts the
prompts and the dashboard can use: same sport or not, the duration ratio, and
the intensity read from the heart-rate zone distribution.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

Verdict = Literal["as_planned", "different_sport", "easier", "harder", "shorter", "longer"]

# Done ÷ planned minutes outside these bounds is shorter / longer than planned.
SHORTER_BELOW = 0.8
LONGER_ABOVE = 1.25

# Heart-rate time (share of the session) that marks the intensity as off-plan. Easy sessions are
# harder than planned with too much time at Z3+ (Z4+); hard ones easier than planned with too little
# at Z3+ (tempo) or Z4+ (threshold, VO2). Warm-ups, recoveries and HR lag dilute hard work, so the
# "easier" bounds are low: they catch a session where the planned work didn't happen at all.
HARDER_IF_Z3_PLUS_ABOVE = {"recovery": 10, "endurance": 25}
HARDER_IF_Z4_PLUS_ABOVE = {"recovery": 5, "endurance": 10, "tempo": 35}
HARDER_IF_Z5_ABOVE = {"threshold": 20}
EASIER_IF_Z3_PLUS_BELOW = {"tempo": 15}
EASIER_IF_Z4_PLUS_BELOW = {"threshold": 8, "vo2": 8}

# The verdict is the first deviation found in this order; `deviations` lists them all.
PRIORITY: tuple[Verdict, ...] = ("different_sport", "easier", "harder", "shorter", "longer")

_ZONE = re.compile(r"(\d+)")


def zone_shares(hr_zone_seconds: Mapping[str, Any] | None) -> dict[str, float] | None:
    """Share of heart-rate time at Z3+, Z4+ and Z5 (percent), or None without zone data."""
    if not hr_zone_seconds:
        return None
    by_zone: dict[int, float] = {}
    for key, seconds in hr_zone_seconds.items():
        match = _ZONE.search(str(key))
        if match and seconds:
            by_zone[int(match.group(1))] = by_zone.get(int(match.group(1)), 0.0) + float(seconds)
    total = sum(by_zone.values())
    if total <= 0:
        return None

    def share(min_zone: int) -> float:
        return round(100 * sum(v for z, v in by_zone.items() if z >= min_zone) / total, 1)

    return {"z3_plus_pct": share(3), "z4_plus_pct": share(4), "z5_pct": share(5)}


def _intensity_check(intensity: str | None, shares: dict[str, float] | None) -> tuple[str | None, str | None]:
    """('easier' | 'harder' | 'as_planned' | None, why it wasn't checked)."""
    if intensity is None:
        return None, "no planned intensity"
    if intensity in ("strength", "mixed"):
        return None, f"planned intensity '{intensity}' isn't read from heart-rate zones"
    if shares is None:
        return None, "no heart-rate zones"
    z3, z4, z5 = shares["z3_plus_pct"], shares["z4_plus_pct"], shares["z5_pct"]
    if z3 > HARDER_IF_Z3_PLUS_ABOVE.get(intensity, 100) or z4 > HARDER_IF_Z4_PLUS_ABOVE.get(intensity, 100):
        return "harder", None
    if z5 > HARDER_IF_Z5_ABOVE.get(intensity, 100):
        return "harder", None
    if z3 < EASIER_IF_Z3_PLUS_BELOW.get(intensity, 0) or z4 < EASIER_IF_Z4_PLUS_BELOW.get(intensity, 0):
        return "easier", None
    return "as_planned", None


def _label(sport: str | None, minutes: float | None, intensity: str | None = None) -> str:
    parts = [str(sport or "session")]
    if minutes:
        parts.append(f"{round(minutes)} min")
    if intensity:
        parts.append(intensity)
    return " ".join(parts)


def compare_to_plan(
    *,
    planned_sport: str,
    planned_minutes: float | None,
    planned_intensity: str | None,
    done_sport: str,
    done_seconds: float | None,
    hr_zone_seconds: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare a done session with the plan row it is linked to.

    Returns the ``verdict`` (``as_planned``, ``different_sport``, ``easier``, ``harder``,
    ``shorter`` or ``longer`` — the most significant difference), every difference in
    ``deviations``, the facts behind them, what couldn't be checked (``not_checked``) and a
    one-line ``summary``.
    """
    done_minutes = round(done_seconds / 60) if done_seconds else None
    sport_match = planned_sport == done_sport
    deviations: list[Verdict] = [] if sport_match else ["different_sport"]
    not_checked: list[str] = []

    ratio = round(done_minutes / planned_minutes, 2) if planned_minutes and done_minutes is not None else None
    if ratio is None:
        not_checked.append("duration: " + ("no planned duration" if not planned_minutes else "no recorded duration"))
    elif ratio < SHORTER_BELOW:
        deviations.append("shorter")
    elif ratio > LONGER_ABOVE:
        deviations.append("longer")

    shares = zone_shares(hr_zone_seconds)
    intensity: str | None = None
    if sport_match:
        intensity, why = _intensity_check(planned_intensity, shares)
        if intensity == "easier":
            deviations.append("easier")
        elif intensity == "harder":
            deviations.append("harder")
        elif why:
            not_checked.append(f"intensity: {why}")

    verdict: Verdict = next((v for v in PRIORITY if v in deviations), "as_planned")
    deviations.sort(key=PRIORITY.index)

    planned_label = _label(planned_sport, planned_minutes, planned_intensity)
    done_label = _label(done_sport, done_minutes)
    facts = []
    if ratio is not None and sport_match:
        facts.append(f"{round(ratio * 100)}% of the planned time")
    if shares is not None:
        facts.append(f"{shares['z3_plus_pct']:g}% of heart-rate time in Z3+, {shares['z4_plus_pct']:g}% in Z4+")
    how = " and ".join("a different sport" if d == "different_sport" else d for d in deviations) or "as planned"
    summary = f"Planned {planned_label}; did {done_label}" + (f" ({'; '.join(facts)})" if facts else "") + f": {how}."

    return {
        "verdict": verdict,
        "deviations": deviations,
        "sport_match": sport_match,
        "planned": {"sport": planned_sport, "minutes": planned_minutes, "intensity": planned_intensity},
        "done": {"sport": done_sport, "minutes": done_minutes, **(shares or {})},
        "duration_ratio": ratio,
        "intensity_vs_plan": intensity,
        "not_checked": not_checked,
        "summary": summary,
    }
