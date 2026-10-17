"""Facts for the weekly review: the plan against what was done, and the trend behind it.

Pure functions over plain rows (``server.weekly_review`` queries them):

* which week a review covers;
* plan adherence — every session that stood in the plan, done (as planned or not, by
  ``analytics.plan_compare``) or missed, and sessions done without one;
* weekly session targets from the athlete's notes against the counts, with how many weeks in a row fell
  short, so a one-off and a pattern look different;
* the week's volume against the weeks before it, and a summary of its grades.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any

# Sports that count towards plans and targets; walks, hikes and the like are "other".
TRAINING_SPORTS = ("swim", "bike", "run", "strength")
DAY = datetime.timedelta(days=1)
PLAN_FROM = {"coach_import": "coach", "manual": "athlete", "suggestion_accepted": "accepted suggestion"}
TREND_BASE_WEEKS = 3  # the reviewed week is compared with the average of up to this many weeks before it
RECENT_WEEKS = 4  # "short in n of the last weeks" counts over this many weeks, the reviewed one included


def review_week(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    """The Monday–Sunday week a review made on *today* covers: the week holding yesterday — on a Monday the
    week that just ended, later in the week the current one so far."""
    yesterday = today - DAY
    start = yesterday - datetime.timedelta(days=yesterday.weekday())
    return start, start + 6 * DAY


def season_weeks(week_start: datetime.date, since: datetime.date, least: int, most: int) -> int:
    """How many weeks a season review spans: from the week holding *since* through the reviewed week,
    kept between *least* and *most*."""
    monday_since = since - datetime.timedelta(days=since.weekday())
    return max(least, min(most, (week_start - monday_since).days // 7 + 1))


def days_left(week_end: datetime.date, today: datetime.date) -> int:
    """Days of the week still to come, today included (0 once the week is over)."""
    return max(0, (week_end - today).days + 1)


def off_plan(comparison: Mapping[str, Any] | None) -> bool:
    """Whether a linked session differs from its plan row (``analytics.plan_compare``'s verdict). Without a
    comparison the link is all there is, and it counts as done as planned."""
    return bool(comparison) and comparison["verdict"] != "as_planned"


def plan_adherence(
    planned: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    today: datetime.date,
    comparisons: Mapping[int, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """What stood in the plan, day by day, against what was done.

    *planned*: the plan that stood — coach or athlete rows not replaced, plus accepted suggestions — with
    ``id, date, sport_type, title, duration_min, intensity, source, activity_id`` (the matched session).
    *sessions*: every session of the week with ``activity_id, date, sport_type, minutes``.
    *comparisons*: ``analytics.plan_compare.compare_to_plan`` for the linked plan rows, by plan row id.
    A planned session with a match is done, and ``vs_plan`` says whether it went as planned or was swapped,
    cut short, eased…: only one done as planned counts in ``done_as_planned`` (and ``completion_pct``), the
    rest in ``done_off_plan``. Without a match it is missed when its day has passed, ``today`` on the day
    itself and ``upcoming`` after it.
    """
    comparisons = comparisons or {}
    by_id = {s["activity_id"]: s for s in sessions}
    items = []
    for p in sorted(planned, key=lambda r: (r["date"], r.get("id") or 0)):
        if p["sport_type"] == "rest":
            continue
        done = by_id.get(p.get("activity_id") or "")
        if done:
            status = "done"
        elif p["date"] < today:
            status = "missed"
        else:
            status = "today" if p["date"] == today else "upcoming"
        item: dict[str, Any] = {
            "date": p["date"],
            "weekday": p["date"].strftime("%a"),
            "sport": p["sport_type"],
            "title": p["title"],
            "planned_min": p.get("duration_min"),
            "intensity": p.get("intensity"),
            "from": PLAN_FROM.get(p.get("source") or "", p.get("source")),
            "status": status,
        }
        if done:
            item["done_min"] = done["minutes"]
            item["activity_id"] = done["activity_id"]
            if p.get("duration_min"):
                item["done_vs_planned_pct"] = round(100 * done["minutes"] / p["duration_min"])
            comparison = comparisons.get(p.get("id"))
            if comparison:
                item["vs_plan"] = {k: comparison[k] for k in ("verdict", "deviations", "summary")}
        items.append(item)

    matched = {i["activity_id"] for i in items if i.get("activity_id")}
    unplanned = [
        {"date": s["date"], "sport": s["sport_type"], "minutes": s["minutes"], "activity_id": s["activity_id"]}
        for s in sorted(sessions, key=lambda s: s["date"])
        if s["activity_id"] not in matched and s["sport_type"] in TRAINING_SPORTS
    ]

    by_sport: dict[str, dict[str, Any]] = {}
    sports = [*TRAINING_SPORTS, *sorted({i["sport"] for i in items} - set(TRAINING_SPORTS))]
    for sport in sports:
        mine = [i for i in items if i["sport"] == sport]
        done_sessions = [s for s in sessions if s["sport_type"] == sport]
        due = [i for i in mine if i["status"] in ("done", "missed")]
        done_items = [i for i in mine if i["status"] == "done"]
        row = {
            "planned": len(mine),
            "planned_due": len(due),
            "done_as_planned": sum(1 for i in done_items if not off_plan(i.get("vs_plan"))),
            "done_off_plan": sum(1 for i in done_items if off_plan(i.get("vs_plan"))),
            "missed": sum(1 for i in mine if i["status"] == "missed"),
            "unplanned": sum(1 for u in unplanned if u["sport"] == sport),
            "sessions": len(done_sessions),
            "planned_min_due": sum(i["planned_min"] or 0 for i in due),
            "done_min": sum(s["minutes"] for s in done_sessions),
        }
        if any(row.values()):
            by_sport[sport] = row

    due_total = sum(r["planned_due"] for r in by_sport.values())
    done_total = sum(r["done_as_planned"] for r in by_sport.values())
    return {
        "sessions": items,
        "unplanned": unplanned,
        "by_sport": by_sport,
        "totals": {
            "planned": sum(r["planned"] for r in by_sport.values()),
            "planned_due": due_total,
            "done_as_planned": done_total,
            "done_off_plan": sum(r["done_off_plan"] for r in by_sport.values()),
            "missed": sum(r["missed"] for r in by_sport.values()),
            "unplanned": len(unplanned),
            "completion_pct": round(100 * done_total / due_total) if due_total else None,
        },
    }


def target_status(targets: dict[str, int], counts_by_week: list[dict[str, int]], left: int = 0) -> list[dict[str, Any]]:
    """Weekly session minimums from the notes (``min_sessions_per_week``) against what was done.

    *counts_by_week*: sessions per sport, oldest week first, the reviewed week last.  *left*: days of the
    reviewed week still to come (at most one session of a sport a day can still be added).
    ``short_streak`` counts the weeks in a row that fell short, ending with the reviewed week.
    """
    out = []
    for sport, target in sorted(targets.items()):
        target = int(target)
        counts = [int(w.get(sport, 0)) for w in counts_by_week] or [0]
        done = counts[-1]
        streak = 0
        for c in reversed(counts):
            if c >= target:
                break
            streak += 1
        recent = counts[-RECENT_WEEKS:]
        out.append(
            {
                "sport": sport,
                "target_per_week": target,
                "done": done,
                "met": done >= target,
                "short_by": max(0, target - done),
                "still_possible": done + left >= target,
                "short_streak_weeks": streak,
                "short_in_recent_weeks": f"{sum(1 for c in recent if c < target)} of {len(recent)}",
                "previous_weeks": counts[:-1],
            }
        )
    return out


def _pct_change(value: float, base: float) -> float | None:
    return round(100 * (value - base) / base) if base else None


def volume_change(weeks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The reviewed week (the last row) against the average of up to three weeks before it that had any
    training: total minutes and load, and minutes per sport.  Rows: ``minutes, load, by_sport{sport: {minutes}}``."""
    if not weeks:
        return None
    this, before = weeks[-1], [w for w in weeks[:-1] if w.get("minutes")][-TREND_BASE_WEEKS:]
    if not before:
        return None
    n = len(before)
    base_minutes = sum(w["minutes"] for w in before) / n
    base_load = sum(w.get("load") or 0 for w in before) / n
    sports = sorted({s for w in [*before, this] for s in (w.get("by_sport") or {})})
    return {
        "compared_with_weeks": n,
        "minutes": {"this_week": this.get("minutes") or 0, "average_before": round(base_minutes)},
        "minutes_change_pct": _pct_change(this.get("minutes") or 0, base_minutes),
        "load": {"this_week": this.get("load") or 0, "average_before": round(base_load)},
        "load_change_pct": _pct_change(this.get("load") or 0, base_load),
        "minutes_by_sport": {
            s: {
                "this_week": ((this.get("by_sport") or {}).get(s) or {}).get("minutes", 0),
                "average_before": round(
                    sum(((w.get("by_sport") or {}).get(s) or {}).get("minutes", 0) for w in before) / n
                ),
            }
            for s in sports
        },
    }


def grade_summary(grades: list[dict[str, Any]]) -> dict[str, Any]:
    """The week's graded sessions: how many, their average score and letters, and the lowest one."""
    graded = [g for g in grades if g.get("overall_score") is not None]
    if not graded:
        return {"graded": 0}
    letters: dict[str, int] = {}
    for g in graded:
        letters[g["letter"]] = letters.get(g["letter"], 0) + 1
    lowest = min(graded, key=lambda g: g["overall_score"])
    return {
        "graded": len(graded),
        "average_score": round(sum(g["overall_score"] for g in graded) / len(graded), 2),
        "letters": dict(sorted(letters.items())),
        "low_confidence": sum(1 for g in graded if g.get("confidence") == "low"),
        "lowest": {k: lowest.get(k) for k in ("activity_id", "date", "sport", "letter", "overall_score")},
    }


def scorecard(adherence: dict[str, Any], targets: list[dict[str, Any]], grades: dict[str, Any]) -> dict[str, Any]:
    """The few numbers the Season page and the Discord message show next to Ember's review."""
    by_target = {t["sport"]: t for t in targets}
    sports = []
    for sport, row in adherence["by_sport"].items():
        if sport not in TRAINING_SPORTS:
            continue
        t = by_target.get(sport)
        sports.append(
            {
                "sport": sport,
                "sessions": row["sessions"],
                "planned_due": row["planned_due"],
                "done_as_planned": row["done_as_planned"],
                "target": t["target_per_week"] if t else None,
                "target_met": t["met"] if t else None,
                "minutes": row["done_min"],
            }
        )
    for t in targets:  # a target sport with nothing planned or done still shows (0 of 3 runs)
        if t["sport"] not in adherence["by_sport"]:
            sports.append(
                {
                    "sport": t["sport"],
                    "sessions": 0,
                    "planned_due": 0,
                    "done_as_planned": 0,
                    "target": t["target_per_week"],
                    "target_met": False,
                    "minutes": 0,
                }
            )
    order = {s: i for i, s in enumerate(TRAINING_SPORTS)}
    return {
        "sports": sorted(sports, key=lambda s: order.get(s["sport"], 99)),
        "completion_pct": adherence["totals"]["completion_pct"],
        "planned_due": adherence["totals"]["planned_due"],
        "done_as_planned": adherence["totals"]["done_as_planned"],
        "grade_average": grades.get("average_score"),
        "graded": grades.get("graded", 0),
    }
