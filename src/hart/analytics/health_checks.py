"""Health-check reminder rules — deterministic, no Claude.

``evaluate`` turns lab results, the season and the athlete notes into the
checks that *should* exist right now, each with a stable ``rule_key``.  The
server stores them (one check per key); a check that stops being produced
has been resolved (e.g. a later panel covered its markers).

Informational only: rationales describe results and timing, never diagnose.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

D = datetime.date
DAY = datetime.timedelta(days=1)

# The regular athlete panel (by catalogue key).
PANEL = (
    "hemoglobin", "hematocrit", "rbc", "wbc", "platelets", "ferritin", "iron", "tsh", "ft4",
    "vitamin_d_25oh", "vitamin_b12", "crp", "creatinine", "egfr", "sodium", "potassium", "alt", "glucose",
    "cholesterol_total", "hdl", "ldl",
)
FULL_PANEL_SHARE = 0.5  # a test date with at least half the panel counts as a full panel
STALE_MARKERS = ("ferritin", "vitamin_d_25oh", "vitamin_b12", "tsh")
MERGE_PANELS_DAYS = 45
FOLD_STALE_DAYS = 60
TREND_MIN_CHANGE = 0.10
OLD_FOLLOWUP_DAYS = 365  # older unresolved results go into the next panel instead

DEFAULTS: dict[str, float] = {
    "health_panel_interval_days": 182,
    "health_panel_before_build_days": 28,
    "health_panel_before_race_days": 70,
    "health_followup_flag_days": 56,
    "health_followup_near_days": 84,
    "health_near_limit_pct": 5.0,
    "health_near_change_pct": 20.0,
    "health_stale_days": 365,
    "health_physio_interval_days": 42,
    "health_vitamin_d_month": 2,  # late winter: 2 (Feb) in the northern hemisphere, 8 (Aug) in the southern
}


@dataclass
class CheckSpec:
    rule_key: str
    kind: str  # lab_panel | follow_up | medical_exam | physio
    title: str
    due_date: D
    markers: list[str] = field(default_factory=list)
    rationale: str = ""


@dataclass
class Context:
    today: D
    results: list[dict[str, Any]]  # lab rows: test_date, marker_key, marker_name, value_num, qualifier, unit, ref_low, ref_high, flag, status
    names: dict[str, str]  # catalogue key → English name
    build_start: D | None = None
    a_race: dict[str, Any] | None = None  # name, race_date, distance
    notes: list[dict[str, Any]] = field(default_factory=list)  # active notes: id, category, title, body, rules, valid_from, created
    physio_done: dict[int, D] = field(default_factory=dict)  # note id → last completed physio check
    t: dict[str, float] = field(default_factory=lambda: dict(DEFAULTS))


def _fmt(d: D) -> str:
    return f"{d.day} {d.strftime('%b %Y')}"


def _num(v: float) -> str:
    return f"{v:g}"


def by_marker(results: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in sorted(results, key=lambda r: r["test_date"]):
        if r.get("marker_key"):
            out.setdefault(r["marker_key"], []).append(r)
    return out


def last_full_panel(results: list[dict[str, Any]]) -> D | None:
    keys_by_date: dict[D, set[str]] = {}
    for r in results:
        if r.get("marker_key") in PANEL:
            keys_by_date.setdefault(r["test_date"], set()).add(r["marker_key"])
    full = [d for d, keys in keys_by_date.items() if len(keys) >= FULL_PANEL_SHARE * len(PANEL)]
    return max(full) if full else None


def trends(results: list[dict[str, Any]], names: dict[str, str]) -> list[dict[str, Any]]:
    """Markers that moved the same way over their last 3 numeric results (≥ 10% overall)."""
    out = []
    for key, rs in by_marker(results).items():
        nums = [r for r in rs if r.get("value_num") is not None and r.get("qualifier") is None]
        if len(nums) < 3:
            continue
        a, b, c = (r["value_num"] for r in nums[-3:])
        if not a or not ((a < b < c) or (a > b > c)) or abs(c - a) / abs(a) < TREND_MIN_CHANGE:
            continue
        direction = "rising" if c > a else "falling"
        unit = nums[-1].get("unit") or ""
        out.append({
            "key": key, "direction": direction, "dates": [r["test_date"] for r in nums[-3:]],
            "values": [a, b, c],
            "text": f"{names.get(key, key)} {direction} over the last 3 tests "
                    f"({_num(a)} → {_num(b)} → {_num(c)} {unit}".rstrip() + ")",
        })
    return out


def _range_text(r: dict[str, Any]) -> str:
    low, high = r.get("ref_low"), r.get("ref_high")
    if low is not None and high is not None:
        return f"{_num(low)}–{_num(high)}"
    if high is not None:
        return f"< {_num(high)}"
    if low is not None:
        return f"> {_num(low)}"
    return "no range printed"


def _planned_from_notes(ctx: Context, series: dict[str, list[dict[str, Any]]]) -> tuple[list[CheckSpec], set[str]]:
    """Follow-ups a note says were planned (rules.planned_labs) with no result since."""
    specs, covered = [], set()
    for note in ctx.notes:
        for i, plan in enumerate((note.get("rules") or {}).get("planned_labs") or []):
            markers = [m for m in plan.get("markers", []) if m]
            if not markers:
                continue
            after = D.fromisoformat(plan["after"]) if plan.get("after") else note.get("valid_from")
            done = all(any(after is None or r["test_date"] > after for r in series.get(m, [])) for m in markers)
            if done:
                continue
            due = D.fromisoformat(plan["due"]) if plan.get("due") else ctx.today
            label = ", ".join(ctx.names.get(m, m) for m in markers)
            specs.append(CheckSpec(
                rule_key=f"planned:{note['id']}:{i}", kind="follow_up", markers=markers, due_date=due,
                title=plan.get("title") or f"Planned re-check: {label}",
                rationale=f"Planned in the note “{note['title']}”; no {label} result since"
                          f"{' ' + _fmt(after) if after else ''}.",
            ))
            covered.update(markers)
    return specs, covered


def _follow_ups(ctx: Context, series: dict[str, list[dict[str, Any]]], covered: set[str],
                old: list[dict[str, Any]]) -> list[CheckSpec]:
    """Follow-ups for flagged / near-limit latest results; results older than a
    year are collected in *old* for the next panel instead."""
    near_pct = ctx.t["health_near_limit_pct"] / 100
    change_pct = ctx.t["health_near_change_pct"] / 100
    groups: dict[D, dict[str, Any]] = {}
    for key, rs in series.items():
        if key in covered:
            continue
        r = rs[-1]
        line = None
        due_days = None
        if r.get("status"):
            unit = r.get("unit") or ""
            line = f"{ctx.names.get(key, key)} {r['value_text']} {unit}".rstrip() + \
                   f" — {'above' if r['status'] == 'high' else 'below'} the lab range ({_range_text(r)})"
            due_days = ctx.t["health_followup_flag_days"]
        elif len(rs) >= 2 and r.get("value_num") is not None and rs[-2].get("value_num"):
            v, prev = r["value_num"], rs[-2]["value_num"]
            high, low = r.get("ref_high"), r.get("ref_low")
            near_high = high is not None and v >= high * (1 - near_pct)
            near_low = low is not None and low > 0 and v <= low * (1 + near_pct)
            moved = abs(v - prev) / abs(prev)
            if (near_high or near_low) and moved > change_pct:
                unit = r.get("unit") or ""
                line = (f"{ctx.names.get(key, key)} {r['value_text']} {unit}".rstrip()
                        + f" — near the {'top' if near_high else 'bottom'} of the range ({_range_text(r)}), "
                        f"{'+' if v > prev else '−'}{moved * 100:.0f}% since {_fmt(rs[-2]['test_date'])}")
                due_days = ctx.t["health_followup_near_days"]
        if line is None:
            continue
        g = groups.setdefault(r["test_date"], {"markers": [], "lines": [], "due": None})
        g["markers"].append(key)
        g["lines"].append(line)
        due = r["test_date"] + int(due_days) * DAY
        g["due"] = due if g["due"] is None else min(g["due"], due)
    specs = []
    for test_date, g in sorted(groups.items()):
        if (ctx.today - test_date).days > OLD_FOLLOWUP_DAYS:
            old.append({"markers": g["markers"], "lines": g["lines"], "test_date": test_date})
            continue
        label = ", ".join(ctx.names.get(m, m) for m in g["markers"][:4]) + ("…" if len(g["markers"]) > 4 else "")
        specs.append(CheckSpec(
            rule_key=f"followup:{test_date}", kind="follow_up", markers=g["markers"], due_date=g["due"],
            title=f"Follow-up: {label} (tests of {_fmt(test_date)})",
            rationale="; ".join(g["lines"]) + ". Worth re-testing and discussing with your doctor.",
        ))
    return specs


def _panels(ctx: Context, trend_list: list[dict[str, Any]], old: list[dict[str, Any]]) -> list[CheckSpec]:
    last = last_full_panel(ctx.results)
    candidates: list[tuple[D, str, str]] = []
    if last is None:
        candidates.append((ctx.today, "panel:none", "No full athlete panel on record yet."))
    else:
        due = last + int(ctx.t["health_panel_interval_days"]) * DAY
        candidates.append((due, f"panel:{last}", f"Regular athlete panel — the last full panel was {_fmt(last)}."))
    if ctx.build_start and ctx.build_start > ctx.today and (last is None or last < ctx.build_start - 90 * DAY):
        due = ctx.build_start - int(ctx.t["health_panel_before_build_days"]) * DAY
        candidates.append((due, f"panel_build:{ctx.build_start}",
                           f"Before Build starts on {_fmt(ctx.build_start)}, so there's time to act on the results."))
    if ctx.a_race and ctx.a_race["race_date"] > ctx.today and (last is None or last < ctx.a_race["race_date"] - 120 * DAY):
        due = ctx.a_race["race_date"] - int(ctx.t["health_panel_before_race_days"]) * DAY
        candidates.append((due, f"panel_race:{ctx.a_race['race_date']}",
                           f"About 10 weeks before {ctx.a_race['name']} ({_fmt(ctx.a_race['race_date'])})."))
    kept: list[list[Any]] = []
    for due, key, reason in sorted(candidates):
        if kept and (due - kept[-1][0]).days <= MERGE_PANELS_DAYS:
            kept[-1][2] += " " + reason
            continue
        kept.append([max(due, ctx.today) if key == "panel:none" else due, key, reason])
    extra = [t["text"] for t in trend_list]
    specs = []
    for i, (due, key, reason) in enumerate(kept):
        rationale = reason + " Markers: blood count, ferritin, iron, TSH + FT4, vitamin D, B12, CRP, creatinine/eGFR, " \
                             "sodium/potassium, ALT, glucose, lipids."
        markers = list(PANEL)
        if i == 0 and extra:
            rationale += " Trends to look at: " + "; ".join(extra) + "."
        if i == 0 and old:
            rationale += " Not re-tested since: " + "; ".join(
                f"{line} (tests of {_fmt(o['test_date'])})" for o in old for line in o["lines"]) + "."
            markers += [m for o in old for m in o["markers"] if m not in markers]
        specs.append(CheckSpec(rule_key=key, kind="lab_panel", title="Athlete blood panel", due_date=due,
                               markers=markers, rationale=rationale))
    return specs


def _stale(ctx: Context, series: dict[str, list[dict[str, Any]]], panels: list[CheckSpec]) -> list[CheckSpec]:
    limit = int(ctx.t["health_stale_days"])
    soon = min((p.due_date for p in panels), default=None)
    specs = []
    for key in STALE_MARKERS:
        rs = series.get(key, [])
        last = rs[-1]["test_date"] if rs else None
        if last is not None and (ctx.today - last).days <= limit:
            continue
        name = ctx.names.get(key, key)
        when = f"last tested {_fmt(last)}" if last else "never tested"
        if key == "vitamin_d_25oh":
            # Lowest in late winter: time the test for that month (health_vitamin_d_month, Feb by default;
            # set 8 for the southern hemisphere).
            month = int(ctx.t.get("health_vitamin_d_month", 2))
            window = {month, month % 12 + 1}
            due = ctx.today if ctx.today.month in window else D(
                ctx.today.year + (ctx.today.month > month), month, 15)
            winter = next((p for p in panels if p.due_date.month in window | {(month + 1) % 12 + 1}
                           and abs((p.due_date - due).days) <= FOLD_STALE_DAYS), None)
            if winter:
                winter.rationale += f" Includes {name} ({when}) — late winter is the right time for it."
                continue
            specs.append(CheckSpec(
                rule_key=f"stale:{key}:{due.year}", kind="follow_up", markers=[key], due_date=due, title=f"{name} test",
                rationale=f"{name} was {when}. Levels are lowest in late winter, so a test then shows whether "
                          "you need to act before the season.",
            ))
            continue
        if soon and (soon - ctx.today).days <= FOLD_STALE_DAYS:
            panels[0].rationale += f" Includes {name} ({when})."
            continue
        specs.append(CheckSpec(
            rule_key=f"stale:{key}:{last}", kind="follow_up", markers=[key], due_date=ctx.today,
            title=f"{name} test", rationale=f"{name} was {when} — relevant for endurance training, worth checking.",
        ))
    return specs


def _pre_race_exam(ctx: Context) -> list[CheckSpec]:
    race = ctx.a_race
    if not race or race.get("distance") != "full" or race["race_date"] <= ctx.today:
        return []
    due = ctx.build_start if ctx.build_start and ctx.build_start < race["race_date"] else race["race_date"] - 180 * DAY
    rationale = (f"Sports medical exam with a resting ECG before the first full-distance race, {race['name']} "
                 f"({_fmt(race['race_date'])}).")
    cardiac = [n for n in ctx.notes if "cardiac" in (n.get("body") or "").lower()]
    if cardiac:
        rationale += (f" The note “{cardiac[0]['title']}” records a cardiac-risk concern — ask the doctor whether "
                      "more (e.g. exercise ECG or echocardiography) is warranted.")
    return [CheckSpec(rule_key=f"prerace_exam:{race['race_date']}", kind="medical_exam",
                      title="Sports medical exam with ECG", due_date=max(due, ctx.today), rationale=rationale)]


def _physio(ctx: Context) -> list[CheckSpec]:
    every = int(ctx.t["health_physio_interval_days"])
    specs = []
    for note in ctx.notes:
        if note.get("category") != "injury" or "physio" not in (note.get("body") or "").lower():
            continue
        anchor = ctx.physio_done.get(note["id"]) or note.get("valid_from") or note.get("created")
        if anchor is None:
            continue
        specs.append(CheckSpec(
            rule_key=f"physio:{note['id']}:{anchor}", kind="physio", title=f"Physio check-in: {note['title']}",
            due_date=anchor + every * DAY,
            rationale=f"Review progress with the physio every {every // 7} weeks while “{note['title']}” is active.",
        ))
    return specs


def evaluate(ctx: Context) -> tuple[list[CheckSpec], list[dict[str, Any]]]:
    """The checks that should exist now, and the marker trends (for charts)."""
    series = by_marker(ctx.results)
    trend_list = trends(ctx.results, ctx.names)
    planned, covered = _planned_from_notes(ctx, series)
    old: list[dict[str, Any]] = []
    follow_ups = _follow_ups(ctx, series, covered, old)
    panels = _panels(ctx, trend_list, old)
    specs = [*panels, *planned, *follow_ups, *_stale(ctx, series, panels),
             *_pre_race_exam(ctx), *_physio(ctx)]
    return specs, trend_list
