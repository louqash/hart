"""Interval breakdown: a session lap by lap, against the workout's targets.

When a structured workout was followed on the watch, every lap belongs to a workout
step (warm-up, work, recovery, cool-down) with a target — watts, heart rate, pace or
cadence. Each work lap is compared with its target (on / under / over), and the reps
are checked for fading. Without a workout, laps are listed as recorded (manual laps,
auto-laps) with no verdict.
"""

from __future__ import annotations

from typing import Any

TOLERANCE = 0.02  # 2 % around the target range still counts as on target
MAX_LAPS = 60
KINDS = {
    "warmup": "warmup",
    "cooldown": "cooldown",
    "active": "work",
    "interval": "work",
    "rest": "recovery",
    "recovery": "recovery",
}
METRIC_FOR_UNIT = {"W": "avg_power", "bpm": "avg_hr", "sec_km": "avg_pace_sec_km", "rpm": "avg_cadence"}


def _kind(lap: dict[str, Any], step: dict[str, Any] | None) -> str | None:
    intensity = (step or {}).get("intensity") or lap.get("intensity")
    return KINDS.get(str(intensity)) if intensity else None


def _default_metric(sport: str, laps: list[dict[str, Any]]) -> str:
    if sport == "bike" and any(lap.get("avg_power") for lap in laps):
        return "avg_power"
    if sport == "run" and any(lap.get("avg_pace_sec_km") for lap in laps):
        return "avg_pace_sec_km"
    return "avg_hr"


def verdict(value: float | None, low: float | None, high: float | None, metric: str) -> tuple[str | None, float | None]:
    """'on' / 'under' / 'over' against a target range, and how far off in % (for pace, 'under' = slower)."""
    if value is None or (low is None and high is None):
        return None, None
    low = low if low is not None else high
    high = high if high is not None else low
    assert low is not None and high is not None
    if metric == "avg_pace_sec_km":  # seconds per km: bigger is slower
        if value > high * (1 + TOLERANCE):
            return "under", round((value / high - 1) * 100, 1)
        if value < low * (1 - TOLERANCE):
            return "over", round((1 - value / low) * 100, 1)
        return "on", 0.0
    if value < low * (1 - TOLERANCE):
        return "under", round((1 - value / low) * 100, 1)
    if value > high * (1 + TOLERANCE):
        return "over", round((value / high - 1) * 100, 1)
    return "on", 0.0


def breakdown(sport: str, laps: list[dict[str, Any]], steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Lap-by-lap view of a session. None when there's nothing to break down (fewer than 2 laps)."""
    if len(laps) < 2:
        return None
    by_step = {s["step_index"]: s for s in steps if s.get("step_index") is not None}
    structured = bool(by_step) and any(lap.get("wkt_step_index") is not None for lap in laps)
    fallback_metric = _default_metric(sport, laps)
    rows: list[dict[str, Any]] = []
    rep = 0
    for lap in laps[:MAX_LAPS]:
        step = by_step.get(lap.get("wkt_step_index")) if structured else None
        kind = _kind(lap, step)
        unit = (step or {}).get("target_unit")
        metric = METRIC_FOR_UNIT.get(unit or "", fallback_metric)
        row: dict[str, Any] = {
            "lap": lap["lap_index"] + 1,
            "kind": kind,
            "step": (step or {}).get("name"),
            "seconds": lap.get("moving_seconds") or lap.get("elapsed_seconds"),
            "power": lap.get("avg_power"),
            "pace_sec_km": round(lap["avg_pace_sec_km"], 1) if lap.get("avg_pace_sec_km") else None,
            "hr": lap.get("avg_hr"),
            "max_hr": lap.get("max_hr"),
            "cadence": lap.get("avg_cadence"),
        }
        if kind == "work":
            rep += 1
            row["rep"] = rep
        if step and step.get("target_type") not in (None, "open"):
            row["target"] = {"low": step.get("target_low"), "high": step.get("target_high"), "unit": unit}
            if unit in METRIC_FOR_UNIT and kind == "work":
                row["verdict"], row["off_pct"] = verdict(
                    lap.get(metric), step.get("target_low"), step.get("target_high"), metric
                )
        rows.append({k: v for k, v in row.items() if v is not None})

    work = [r for r in rows if r.get("kind") == "work"]
    summary: dict[str, Any] = {"source": "workout" if structured else "laps", "laps": len(rows), "work_reps": len(work)}
    judged = [r for r in work if r.get("verdict")]
    if judged:
        summary["on_target"] = sum(r["verdict"] == "on" for r in judged)
        summary["under"] = sum(r["verdict"] == "under" for r in judged)
        summary["over"] = sum(r["verdict"] == "over" for r in judged)
    key = {"avg_power": "power", "avg_pace_sec_km": "pace_sec_km", "avg_hr": "hr", "avg_cadence": "cadence"}[
        METRIC_FOR_UNIT.get((work[0].get("target") or {}).get("unit", ""), fallback_metric) if work else fallback_metric
    ]
    values = [r[key] for r in work if r.get(key)]
    if len(values) >= 3:
        # Fading: last rep vs first, in the direction that means "worse" (less power, slower pace).
        change = (values[-1] / values[0] - 1) * 100
        summary["last_vs_first_rep_pct"] = round(change, 1)
        summary["metric"] = key
    hrs = [r["hr"] for r in work if r.get("hr")]
    if len(hrs) >= 3:
        summary["hr_drift_bpm"] = round(hrs[-1] - hrs[0])
    recoveries = [r["seconds"] for r in rows if r.get("kind") == "recovery" and r.get("seconds")]
    if recoveries:
        summary["avg_recovery_seconds"] = round(sum(recoveries) / len(recoveries))
    return {"summary": summary, "laps": rows}
