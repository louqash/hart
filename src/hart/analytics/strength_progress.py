"""Strength progression per exercise (the Strength page).

Per exercise and session: the top set (heaviest weight, most reps at it), the
number of working sets, volume (reps × kg) and an estimated one-rep max
(Epley, only from sets of 1–12 reps with a load).  Bodyweight exercises are
tracked by reps.  Walking, warm-ups and unlabelled sets are left out — they
aren't lifts.
"""

from __future__ import annotations

import datetime
from typing import Any

from hart.storage.database import Database

NOT_LIFTS = frozenset({"RUN", "WARM_UP", "CARDIO", "WALK"})
# A name that is just its category with the default implement in front is the same lift
# ("BARBELL_DEADLIFT" = "DEADLIFT"); other prefixes change the lift or the load (DUMBBELL_, ROMANIAN_).
DEFAULT_IMPLEMENTS = ("BARBELL_",)
ALIASES_KEY = "exercise_aliases"
E1RM_MAX_REPS = 12
BLOCK_WEEKS = 8


def _plain(text: str) -> str:
    return text[:-1] if text.endswith("S") else text


def exercise_key(name: str | None, category: str | None, aliases: dict[str, str] | None = None) -> str | None:
    """One key per lift: Garmin records a set by exercise name, or only by category when it
    isn't sure, so "BARBELL_DEADLIFT" and a category-only "DEADLIFT" must count as one lift.
    *aliases* (Strength page → "Same as…") merges anything else."""
    if category in NOT_LIFTS:
        return None
    key = name or category
    if not key:
        return None
    if name and category:
        base = name
        for prefix in DEFAULT_IMPLEMENTS:
            if base.startswith(prefix):
                base = base[len(prefix) :]
        if _plain(base) == _plain(category):
            key = category
    seen = set()
    while aliases and key in aliases and key not in seen:  # follow chains, stop on cycles
        seen.add(key)
        key = aliases[key]
    return key


def exercise_label(key: str) -> str:
    return key.replace("_", " ").title()


def load_aliases(db: Database) -> dict[str, str]:
    from hart.server import state

    return state.get_setting(db, ALIASES_KEY, {}) or {}


def e1rm(weight: float | None, reps: int | None) -> float | None:
    if not weight or not reps or reps < 1 or reps > E1RM_MAX_REPS:
        return None
    return round(weight * (1 + reps / 30), 1)


def progression(db: Database, today: datetime.date) -> dict[str, Any]:
    rows = db.fetchall(
        "SELECT s.activity_id, CAST(a.start_time AS DATE) AS day, s.exercise_name, s.exercise_category, "
        "s.repetitions, s.weight_kg FROM strength_sets s JOIN activities a USING (activity_id) "
        "WHERE s.set_type = 'active' ORDER BY a.start_time, s.set_index"
    )
    aliases = load_aliases(db)
    sessions: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for activity_id, day, name, category, reps, weight in rows:
        key = exercise_key(name, category, aliases)
        if key is None:
            continue
        s = sessions.setdefault(key, {}).setdefault(
            (activity_id, day.isoformat()),
            {
                "activity_id": activity_id,
                "date": day,
                "sets": 0,
                "volume": 0.0,
                "top_kg": None,
                "top_reps": None,
                "e1rm": None,
                "max_reps": None,
            },
        )
        s["sets"] += 1
        s["volume"] += (reps or 0) * (weight or 0)
        s["max_reps"] = max(s["max_reps"] or 0, reps or 0) or None
        if weight is not None and (
            s["top_kg"] is None
            or weight > s["top_kg"]
            or (weight == s["top_kg"] and (reps or 0) > (s["top_reps"] or 0))
        ):
            s["top_kg"], s["top_reps"] = weight, reps
        est = e1rm(weight, reps)
        if est and (s["e1rm"] is None or est > s["e1rm"]):
            s["e1rm"] = est

    block_start = today - datetime.timedelta(weeks=BLOCK_WEEKS)
    exercises = []
    for key, by_session in sessions.items():
        history = sorted(by_session.values(), key=lambda s: s["date"])
        for h in history:
            h["volume"] = round(h["volume"])
        bodyweight = all(not h["top_kg"] for h in history)
        metric = "max_reps" if bodyweight else "e1rm"
        latest = history[-1]
        in_block = [h for h in history if h["date"] >= block_start and h.get(metric)]
        change = None
        if len(in_block) >= 2 and in_block[0][metric]:
            change = round((in_block[-1][metric] / in_block[0][metric] - 1) * 100, 1)
        exercises.append(
            {
                "key": key,
                "label": exercise_label(key),
                "bodyweight": bodyweight,
                "metric": metric,
                "sessions": len(history),
                "first": history[0]["date"],
                "last": latest["date"],
                "latest": latest,
                "best": max((h[metric] or 0) for h in history) or None,
                "block_sessions": len([h for h in history if h["date"] >= block_start]),
                "block_change_pct": change,
                "history": history,
            }
        )
    # Recently trained and frequent first.
    exercises.sort(key=lambda e: (e["last"], e["sessions"]), reverse=True)
    return {"today": today, "block_start": block_start, "exercises": exercises}


def weekly_sessions(db: Database, today: datetime.date, weeks: int = BLOCK_WEEKS) -> list[dict[str, Any]]:
    monday = today - datetime.timedelta(days=today.weekday())
    start = monday - datetime.timedelta(weeks=weeks - 1)
    counts = dict(
        db.fetchall(
            "SELECT CAST(date_trunc('week', start_time) AS DATE), count(*) FROM activities "
            "WHERE sport_type = 'strength' AND start_time >= ? GROUP BY 1",
            [start],
        )
    )
    return [
        {"week": start + datetime.timedelta(weeks=i), "sessions": counts.get(start + datetime.timedelta(weeks=i), 0)}
        for i in range(weeks)
    ]
