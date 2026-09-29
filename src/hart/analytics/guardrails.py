"""Hard limits for suggested sessions.

Claude is told these limits up front; this module checks its answer
afterwards, so a suggestion that breaks one is repaired or rejected instead
of shown.  Everything here is deterministic.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

INTENSITY_ORDER = {"recovery": 0, "endurance": 1, "strength": 1, "tempo": 2, "threshold": 3, "mixed": 3, "vo2": 4}
HARD_INTENSITIES = frozenset({"threshold", "vo2", "mixed"})
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
COMEBACK_CAP = 1.25
RECOMMENDATIONS_WITH_PLAN = frozenset({"as_planned", "modify", "replace", "rest"})
RECOMMENDATIONS_NO_PLAN = frozenset({"free_choice", "rest"})


@dataclass
class GuardContext:
    date: datetime.date
    readiness: str  # green | amber | red | unknown
    coach_sessions: list[dict[str, Any]] = field(default_factory=list)  # plan rows for the date
    rules: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # (note title, rules)
    week_counts: dict[str, int] = field(default_factory=dict)  # done + planned this week, other than the date's plan
    comeback: bool = False
    longest_min_14d: dict[str, int] = field(default_factory=dict)  # per sport, event days excluded
    injury_active: bool = False
    required_today: dict[str, str] = field(default_factory=dict)  # sport → why it can't wait (weekly targets)


def limits(ctx: GuardContext) -> dict[str, Any]:
    """The limits in a form Claude can read before answering."""
    out: dict[str, Any] = {
        "readiness": ctx.readiness,
        "allowed_recommendations": sorted(RECOMMENDATIONS_WITH_PLAN if ctx.coach_sessions else RECOMMENDATIONS_NO_PLAN),
        "sessions_this_week_so_far": ctx.week_counts,
        "note_rules": [{"note": title, "rules": rules} for title, rules in ctx.rules],
    }
    if ctx.readiness == "red":
        out["red_readiness"] = (
            "no threshold, vo2 or mixed sessions; recommendation must be modify, replace or rest "
            "(free_choice or rest without a coach plan)"
        )
    if ctx.coach_sessions:
        out["coach_plan"] = "must not add duration or intensity beyond the coach's plan for this day"
        if not (ctx.readiness == "red" or ctx.injury_active):
            out["replace"] = "only with your case against the coach's session in coach_take (stance partly/disagree)"
    if ctx.required_today and not ctx.coach_sessions:
        out["required_today"] = {
            sport: f"{why} — include a {sport} session (rest only if readiness is red)"
            for sport, why in ctx.required_today.items()
        }
    if ctx.comeback:
        out["comeback_max_duration_min"] = {
            sport: int(minutes * COMEBACK_CAP) for sport, minutes in ctx.longest_min_14d.items()
        }
        out["comeback_note"] = (
            "comeback phase: no session longer than 1.25 x the longest session of the same "
            "sport in the last 14 days; a sport with no session in 14 days has no cap listed, "
            "keep it short and easy"
        )
    return out


def _level(intensity: str | None) -> int:
    return INTENSITY_ORDER.get(intensity or "", 1)


def check(recommendation: str, sessions: list[dict[str, Any]], ctx: GuardContext, *, argued: bool = False) -> list[str]:
    """Violations of the hard limits; empty when the suggestion is acceptable. *argued*: the suggestion
    makes a case against the coach's session (its coach_take), which allows replacing it."""
    problems: list[str] = []
    has_plan = bool(ctx.coach_sessions)
    allowed = RECOMMENDATIONS_WITH_PLAN if has_plan else RECOMMENDATIONS_NO_PLAN
    if recommendation not in allowed:
        problems.append(
            f"recommendation '{recommendation}' is not allowed "
            f"{'when the coach has planned this day' if has_plan else 'without a coach plan'} "
            f"(use one of: {', '.join(sorted(allowed))})"
        )
    if recommendation == "rest" and sessions:
        problems.append("recommendation 'rest' must not list sessions")
    if recommendation != "rest" and not sessions:
        problems.append(f"recommendation '{recommendation}' needs at least one session")
    if ctx.readiness == "red" and recommendation == "as_planned":
        problems.append("readiness is red: the coach's session must be modified, replaced or skipped")
    if recommendation == "replace" and not (ctx.readiness == "red" or ctx.injury_active or argued):
        problems.append(
            "'replace' needs red readiness, an injury, or your case against the coach's session in coach_take "
            "(stance 'partly' or 'disagree') — otherwise use 'modify'"
        )

    if has_plan and recommendation in ("modify", "replace"):
        plan_min = sum(s.get("duration_min") or 0 for s in ctx.coach_sessions)
        got_min = sum(s.get("duration_min") or 0 for s in sessions)
        if plan_min and got_min > plan_min:
            problems.append(f"sessions total {got_min} min, more than the coach's {plan_min} min")
        plan_levels = [_level(s.get("intensity")) for s in ctx.coach_sessions if s.get("intensity")]
        if plan_levels and any(_level(s.get("intensity")) > max(plan_levels) for s in sessions):
            problems.append("a session is harder than anything the coach planned for this day")

    # Weekly targets from notes (min_sessions_per_week): a session that can't
    # fit in the rest of the week must happen today — unless the coach planned
    # the day (the coach's plan takes priority) or readiness is red and it's rest.
    if ctx.required_today and not has_plan:
        suggested = {s.get("sport_type") for s in sessions}
        for sport, why in ctx.required_today.items():
            if sport in suggested or (recommendation == "rest" and ctx.readiness == "red"):
                continue
            problems.append(f"{why}: the suggestion must include a {sport} session")

    weekday = WEEKDAYS[ctx.date.weekday()]
    added: dict[str, int] = {}
    for s in sessions:
        sport = s.get("sport_type") or "other"
        intensity = s.get("intensity")
        minutes = s.get("duration_min") or 0
        name = f"{sport} '{s.get('title', '')}'"
        added[sport] = added.get(sport, 0) + 1

        if ctx.readiness == "red" and intensity in HARD_INTENSITIES:
            problems.append(f"{name}: readiness is red, so no {intensity} work")

        for title, rules in ctx.rules:
            if sport in (rules.get("forbid_sports") or []):
                problems.append(f"{name}: '{title}' forbids {sport}")
            cap = (rules.get("max_duration_min") or {}).get(sport)
            if cap is not None and minutes > cap:
                problems.append(f"{name}: {minutes} min is over the {cap} min limit from '{title}'")
            max_int = (rules.get("max_intensity") or {}).get(sport)
            if max_int and _level(intensity) > _level(max_int):
                problems.append(f"{name}: intensity {intensity} is above {max_int} allowed by '{title}'")
            days = (rules.get("allowed_weekdays") or {}).get(sport)
            if days is not None and weekday not in [d.lower()[:3] for d in days]:
                problems.append(f"{name}: '{title}' allows {sport} only on {', '.join(days)}")
            per_week = (rules.get("max_sessions_per_week") or {}).get(sport)
            if per_week is not None and ctx.week_counts.get(sport, 0) + added[sport] > per_week:
                problems.append(
                    f"{name}: would make {ctx.week_counts.get(sport, 0) + added[sport]} {sport} sessions this week, "
                    f"'{title}' allows {per_week}"
                )

        coach_planned = recommendation == "as_planned"
        longest = ctx.longest_min_14d.get(sport)
        if ctx.comeback and not coach_planned and longest and minutes > longest * COMEBACK_CAP:
            problems.append(
                f"{name}: {minutes} min is over the comeback cap of {int(longest * COMEBACK_CAP)} min "
                f"(1.25 x the longest {sport} in 14 days, {longest} min)"
            )
    return problems
