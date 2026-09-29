"""Daily steps as readiness context.

Steps don't change the readiness level — Garmin's load already counts runs and
rides — but a day far off the athlete's usual is worth knowing: a long walk or
hike is fatigue the training load doesn't see, and a very low day often means
travel or illness.
"""

from __future__ import annotations

import statistics
from typing import Any

MIN_HISTORY = 7  # days with a count needed for a baseline
HIGH_RATIO = 1.6
HIGH_MIN_STEPS = 15000
LOW_RATIO = 0.4


def step_context(steps: int | None, history: list[int]) -> dict[str, Any] | None:
    """Yesterday's *steps* against the median of the previous days' counts (*history*)."""
    if steps is None:
        return None
    values = [v for v in history if v is not None]
    if len(values) < MIN_HISTORY:
        return {"steps": steps, "usual": None, "flag": None, "note": "Not enough step history for a baseline yet."}
    usual = int(statistics.median(values))
    ratio = steps / usual if usual else None
    flag = None
    note = f"{steps:,} steps vs a usual {usual:,}."
    if ratio is not None and ratio >= HIGH_RATIO and steps >= HIGH_MIN_STEPS:
        flag = "high"
        note += " A big day on foot — extra fatigue the training load doesn't count."
    elif ratio is not None and ratio <= LOW_RATIO:
        flag = "low"
        note += " Much less than usual — travel, illness, or the watch off part of the day?"
    return {"steps": steps, "usual": usual, "ratio": round(ratio, 2) if ratio else None, "flag": flag, "note": note}
