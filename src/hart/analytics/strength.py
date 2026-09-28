"""Strength-training analytics.

Parses Garmin exercise-set payloads into :class:`StrengthSet` rows and
lists a session set by set (exercise, reps, weight, rest).
"""

from __future__ import annotations

import datetime
from typing import Any

from hart.models.activity import StrengthSet


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_garmin_exercise_sets(payload: dict[str, Any] | None) -> list[StrengthSet]:
    """Convert a Garmin ``/activity/{id}/exerciseSets`` response into sets.

    Garmin reports weight in grams (``-1`` on rest sets) and a list of
    candidate exercises with auto-detect probabilities; the top candidate is
    kept.  Exercises edited in Garmin Connect come back at probability 100.
    """
    if not payload:
        return []

    sets: list[StrengthSet] = []
    for idx, raw in enumerate(payload.get("exerciseSets") or []):
        set_type = str(raw.get("setType") or "").lower() or "active"

        weight_g = raw.get("weight")
        weight_kg: float | None = None
        if weight_g is not None and weight_g >= 0:
            weight_kg = round(float(weight_g) / 1000.0, 2)

        category = name = None
        confidence: float | None = None
        exercises = [e for e in raw.get("exercises") or [] if isinstance(e, dict)]
        if exercises:
            top = max(exercises, key=lambda e: e.get("probability") or 0)
            category = top.get("category")
            if category == "UNKNOWN":
                category = None
            name = top.get("name")
            confidence = top.get("probability")

        sets.append(StrengthSet(
            set_index=idx,
            set_type=set_type,
            start_time=_parse_ts(raw.get("startTime")),
            duration_sec=raw.get("duration"),
            repetitions=raw.get("repetitionCount"),
            weight_kg=weight_kg,
            exercise_category=category,
            exercise_name=name,
            exercise_confidence=confidence,
        ))
    return sets


def _parse_ts(val: Any) -> datetime.datetime | None:
    if not val:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(val))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def exercise_label(category: str | None, name: str | None) -> str:
    """Human-readable exercise label, e.g. ``"Barbell Deadlift"``."""
    raw = _str(name) or _str(category) or "unknown"
    return raw.replace("_", " ").title()


def summarize_strength_sets(sets: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe a session set by set, in the order performed.

    *sets* are ``strength_sets`` rows (dicts).  Each active set carries its
    exercise, per-exercise set number, reps, weight, duration, and the rest
    that followed it — order is preserved so supersets stay visible.
    """
    ordered = sorted(sets, key=lambda s: s.get("set_index") or 0)

    out: list[dict[str, Any]] = []
    set_numbers: dict[str, int] = {}
    for s in ordered:
        duration = _num(s.get("duration_sec"))
        if s.get("set_type") == "rest":
            if out:
                out[-1]["rest_after_sec"] = round(duration) if duration is not None else None
            continue
        label = exercise_label(s.get("exercise_category"), s.get("exercise_name"))
        set_numbers[label] = set_numbers.get(label, 0) + 1
        reps = _num(s.get("repetitions"))
        out.append({
            "exercise": label,
            "set": set_numbers[label],
            "reps": int(reps) if reps is not None else None,
            "weight_kg": _num(s.get("weight_kg")),
            "duration_sec": round(duration) if duration is not None else None,
            "rest_after_sec": None,
        })

    return {"active_sets": len(out), "sets": out}


def _str(val: Any) -> str | None:
    """Return *val* if it is a non-empty string (pandas gives NaN for NULL)."""
    return val if isinstance(val, str) and val else None


def _num(val: Any) -> float | None:
    """Coerce to float, treating None/NaN (from pandas rows) as missing."""
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return None if f != f else f
