"""Sauna sessions and daily habits: definitions, summaries and next-morning comparisons.

Pure functions over plain data. A habit on day D is compared with the night that follows it, i.e. the
sleep / HRV / resting HR stored for D + 1 (the date readiness uses for "last night").

The comparison is an association, not a cause: sauna days are often training days, late meals often
follow late sessions. It reports the counts so a reader can tell a pattern from noise.
"""

from __future__ import annotations

import datetime
import re
import unicodedata
from collections.abc import Iterable, Mapping
from statistics import mean
from typing import Any

D = datetime.date
DAY = datetime.timedelta(days=1)

MAX_HABITS = 20
MIN_DAYS = 4  # each side of a comparison needs at least this many nights
SAUNA_KINDS = ("finnish", "infrared", "steam", "other")
# Next-morning outcomes: key → (label, higher is better)
OUTCOMES: dict[str, tuple[str, bool]] = {
    "hrv_last_night_ms": ("HRV last night (ms)", True),
    "resting_hr": ("Resting HR (bpm)", False),
    "sleep_score": ("Sleep score", True),
    "sleep_hours": ("Sleep (h)", True),
}


def habit_key(label: str) -> str:
    """A stable key for a habit label: ``"🌙 Late meal"`` → ``"late_meal"``, ``"Późny posiłek"`` → ``"pozny_posilek"``."""
    text = label.lower().replace("ł", "l")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "_".join(re.findall(r"[a-z0-9]+", text))[:40]


def parse_habits(text: str) -> list[dict[str, str]]:
    """The habit list from the Settings text: comma- or line-separated labels (an emoji in front is kept)."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in re.split(r"[,\n]", text or ""):
        label = " ".join(raw.split())[:60]
        key = habit_key(label)
        if not key or key in seen or key == "sauna":  # sauna has its own log
            continue
        seen.add(key)
        out.append({"key": key, "label": label})
    return out[:MAX_HABITS]


def sauna_summary(sessions: Iterable[Mapping[str, Any]], today: D) -> dict[str, Any]:
    """Sessions and minutes in the last 7 and 28 days (today included), and the last session."""
    items = [s for s in sessions if s["date"] <= today]

    def window(days: int) -> dict[str, Any]:
        inside = [s for s in items if s["date"] > today - days * DAY]
        return {"sessions": len(inside), "minutes": sum(int(s["duration_min"] or 0) for s in inside)}

    last = max((s["date"] for s in items), default=None)
    last_28 = window(28)
    return {
        "last_7_days": window(7),
        "last_28_days": last_28 | {"per_week": round(last_28["sessions"] / 4, 1)},
        "last_session": last,
        "days_since_last": (today - last).days if last else None,
    }


def next_morning_comparison(
    event_days: set[D],
    observed_days: Iterable[D],
    outcomes: Mapping[D, Mapping[str, float | None]],
) -> dict[str, Any]:
    """Average next-morning values after days with the event vs observed days without it.

    *observed_days* are the days we know about (a check-in was answered, or the sauna log was being kept);
    *outcomes* maps a date to that morning's values (keys of ``OUTCOMES``). A metric gets a ``difference``
    only when both sides have at least ``MIN_DAYS`` nights with a value.
    """
    observed = sorted(set(observed_days))
    result: dict[str, Any] = {
        "days_with": sum(1 for d in observed if d in event_days),
        "days_without": sum(1 for d in observed if d not in event_days),
        "metrics": {},
    }
    for key, (label, higher_better) in OUTCOMES.items():
        with_values: list[float] = []
        without_values: list[float] = []
        for d in observed:
            value = (outcomes.get(d + DAY) or {}).get(key)
            if value is None:
                continue
            (with_values if d in event_days else without_values).append(float(value))
        entry: dict[str, Any] = {
            "label": label,
            "higher_is_better": higher_better,
            "with": {"nights": len(with_values), "mean": round(mean(with_values), 1) if with_values else None},
            "without": {
                "nights": len(without_values),
                "mean": round(mean(without_values), 1) if without_values else None,
            },
            "difference": None,
        }
        if len(with_values) >= MIN_DAYS and len(without_values) >= MIN_DAYS:
            entry["difference"] = round(mean(with_values) - mean(without_values), 1)
        result["metrics"][key] = entry
    return result
