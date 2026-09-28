"""Deterministic morning readiness.

Readiness is green / amber / red / unknown.  It's never guessed: an input
without data is skipped, a baseline needs enough history, and "green"
requires sleep plus HRV or the recovery score.  Every rule that fires is
reported so the UI can explain the colour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Any

LEVELS = ("green", "amber", "red")


@dataclass
class ReadinessInputs:
    sleep_hours: float | None = None
    sleep_score: float | None = None
    hrv_last_night: float | None = None
    hrv_history: list[float] = field(default_factory=list)  # nightly values, previous N days
    resting_hr: float | None = None
    rhr_history: list[float] = field(default_factory=list)
    recovery_score: float | None = None
    body_battery: float | None = None
    training_readiness: float | None = None
    tsb_yesterday: float | None = None
    critical_anomalies: list[str] = field(default_factory=list)
    illness: str | None = None  # label of an active illness annotation
    injury: str | None = None  # label of an active injury annotation


def _baseline(history: list[float], min_days: int) -> float | None:
    values = [v for v in history if v is not None]
    return round(mean(values), 1) if len(values) >= min_days else None


def compute_readiness(inp: ReadinessInputs, t: dict[str, float]) -> dict[str, Any]:
    hits: list[dict[str, str]] = []

    def hit(level: str, rule: str, message: str) -> None:
        hits.append({"level": level, "rule": rule, "message": message})

    hrv_base = _baseline(inp.hrv_history, int(t["ready_baseline_min_days"]))
    rhr_base = _baseline(inp.rhr_history, int(t["ready_baseline_min_days"]))
    hrv_pct = None
    if inp.hrv_last_night is not None and hrv_base:
        hrv_pct = round((inp.hrv_last_night / hrv_base - 1) * 100, 1)
        if hrv_pct < t["ready_hrv_red_pct"]:
            hit("red", "hrv", f"HRV {inp.hrv_last_night:.0f} ms is {hrv_pct:+.0f}% vs baseline {hrv_base:.0f}")
        elif hrv_pct < t["ready_hrv_amber_pct"]:
            hit("amber", "hrv", f"HRV {inp.hrv_last_night:.0f} ms is {hrv_pct:+.0f}% vs baseline {hrv_base:.0f}")

    rhr_delta = None
    if inp.resting_hr is not None and rhr_base:
        rhr_delta = round(inp.resting_hr - rhr_base, 1)
        if rhr_delta > t["ready_rhr_red_bpm"]:
            hit("red", "rhr", f"Resting HR {inp.resting_hr:.0f} is {rhr_delta:+.0f} bpm vs baseline {rhr_base:.0f}")
        elif rhr_delta > t["ready_rhr_amber_bpm"]:
            hit("amber", "rhr", f"Resting HR {inp.resting_hr:.0f} is {rhr_delta:+.0f} bpm vs baseline {rhr_base:.0f}")

    if inp.sleep_hours is not None:
        if inp.sleep_hours < t["ready_sleep_red_h"]:
            hit("red", "sleep", f"Slept {inp.sleep_hours:.1f} h")
        elif inp.sleep_hours < t["ready_sleep_amber_h"]:
            hit("amber", "sleep", f"Slept {inp.sleep_hours:.1f} h")

    if inp.recovery_score is not None:
        if inp.recovery_score < t["ready_recovery_red"]:
            hit("red", "recovery", f"Recovery score {inp.recovery_score:.0f}")
        elif inp.recovery_score < t["ready_recovery_amber"]:
            hit("amber", "recovery", f"Recovery score {inp.recovery_score:.0f}")

    if inp.tsb_yesterday is not None:
        if inp.tsb_yesterday < t["ready_tsb_red"]:
            hit("red", "tsb", f"Form (TSB) {inp.tsb_yesterday:.0f}")
        elif inp.tsb_yesterday < t["ready_tsb_amber"]:
            hit("amber", "tsb", f"Form (TSB) {inp.tsb_yesterday:.0f}")

    if inp.training_readiness is not None and inp.training_readiness < t["ready_training_readiness_amber"]:
        hit("amber", "training_readiness", f"Garmin Training Readiness {inp.training_readiness:.0f}")

    for description in inp.critical_anomalies:
        hit("red", "anomaly", f"Critical anomaly: {description}")
    if inp.illness:
        hit("red", "illness", f"Illness: {inp.illness}")
    if inp.injury:
        hit("amber", "injury", f"Injury period: {inp.injury}")

    has_minimum = inp.sleep_hours is not None and (hrv_pct is not None or inp.recovery_score is not None)
    if any(h["level"] == "red" for h in hits):
        level = "red"
    elif any(h["level"] == "amber" for h in hits):
        level = "amber"
    elif has_minimum:
        level = "green"
    else:
        level = "unknown"

    missing = []
    if inp.sleep_hours is None:
        missing.append("sleep")
    if inp.hrv_last_night is None:
        missing.append("HRV")
    elif hrv_base is None:
        missing.append("HRV baseline (needs more nights)")
    if inp.recovery_score is None:
        missing.append("recovery score")

    # Plain-language explanation of each gap and what fills it (shown when readiness is unknown).
    need = int(t["ready_baseline_min_days"])
    window = int(t["ready_baseline_days"])
    why: list[dict[str, str]] = []
    if inp.sleep_hours is None:
        why.append({"input": "Sleep", "why": "No sleep record for last night yet.",
                    "fix": "Either the watch wasn't worn overnight, or Garmin hasn't got the data yet — open Garmin "
                           "Connect on your phone so the watch uploads, then press Sync (the morning check also "
                           "runs every 15 min until 10:30)."})
    if inp.hrv_last_night is None and inp.sleep_hours is not None:
        why.append({"input": "HRV", "why": "Sleep was recorded but Garmin has no overnight HRV for it.",
                    "fix": "Overnight HRV needs the watch worn snugly for the whole night; one short or loose "
                           "night leaves a gap."})
    elif inp.hrv_last_night is not None and hrv_base is None:
        why.append({"input": "HRV baseline",
                    "why": f"Last night's HRV is there, but the baseline needs {need} of the last {window} nights "
                           f"with HRV — you have {len(inp.hrv_history)}.",
                    "fix": f"Wear the watch at night; the baseline appears after "
                           f"{max(need - len(inp.hrv_history), 0)} more night(s)."})
    if inp.recovery_score is None and inp.sleep_hours is None:
        why.append({"input": "Recovery score", "why": "Computed from last night's sleep, HRV and Body Battery.",
                    "fix": "It appears after the sync that brings last night's sleep."})

    if level == "unknown":
        reason = "Not enough data: " + ", ".join(missing) if missing else "Not enough data"
    elif hits:
        reason = hits[0]["message"]
    else:
        reason = "All inputs within normal range"

    return {
        "level": level,
        "reason": reason,
        "hits": hits,
        "missing": missing,
        "missing_detail": why,
        "needs": "Readiness needs last night's sleep plus HRV (with a baseline) or a recovery score.",
        "inputs": {
            "sleep_hours": inp.sleep_hours,
            "sleep_score": inp.sleep_score,
            "hrv_last_night": inp.hrv_last_night,
            "hrv_baseline": hrv_base,
            "hrv_pct": hrv_pct,
            "resting_hr": inp.resting_hr,
            "rhr_baseline": rhr_base,
            "rhr_delta": rhr_delta,
            "recovery_score": inp.recovery_score,
            "body_battery": inp.body_battery,
            "training_readiness": inp.training_readiness,
            "tsb_yesterday": inp.tsb_yesterday,
        },
    }
