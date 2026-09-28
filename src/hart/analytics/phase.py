"""Season structure.

* Layoff detection: the injury gap, the no-watch period, the comeback end.
* Phase calendar generator: comeback → base blocks → build → peak → taper →
  race → transition, aligned to Monday weeks.
* Observed training state from the load curve, and flags when it doesn't
  fit the planned phase.

Pure functions: callers pass rows and thresholds in.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

# Walks, hikes and e-bike rides are stored as "other" and don't count.
TRAINING_SPORTS = ("swim", "bike", "run", "strength")

MIN_LAYOFF_DAYS = 21
MIN_NO_DEVICE_DAYS = 14
COMEBACK_WEEKS = 8


@dataclass(frozen=True)
class Layoff:
    last_activity_date: datetime.date  # the injury happened after this session
    return_date: datetime.date  # first training session after the gap
    gap_days: int


@dataclass(frozen=True)
class DetectedSeason:
    layoff: Layoff | None
    no_device: tuple[datetime.date, datetime.date] | None
    comeback_end: datetime.date | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "layoff": None
            if self.layoff is None
            else {
                "last_activity_date": self.layoff.last_activity_date.isoformat(),
                "return_date": self.layoff.return_date.isoformat(),
                "gap_days": self.layoff.gap_days,
            },
            "no_device": None
            if self.no_device is None
            else {
                "start": self.no_device[0].isoformat(),
                "end": self.no_device[1].isoformat(),
            },
            "comeback_end": None if self.comeback_end is None else self.comeback_end.isoformat(),
        }


def find_longest_layoff(
    activity_dates: list[datetime.date],
    min_gap_days: int = MIN_LAYOFF_DAYS,
) -> Layoff | None:
    """Longest gap between consecutive training days of at least *min_gap_days*.

    ``gap_days`` counts the days without training between the two sessions.
    """
    dates = sorted(set(activity_dates))
    best: Layoff | None = None
    for prev, nxt in zip(dates, dates[1:]):
        gap = (nxt - prev).days - 1
        if gap >= min_gap_days and (best is None or gap > best.gap_days):
            best = Layoff(last_activity_date=prev, return_date=nxt, gap_days=gap)
    return best


def find_longest_missing_run(
    present_dates: list[datetime.date],
    start: datetime.date,
    end: datetime.date,
    min_days: int = MIN_NO_DEVICE_DAYS,
) -> tuple[datetime.date, datetime.date] | None:
    """Longest run of consecutive dates in [start, end] missing from *present_dates*."""
    present = set(present_dates)
    best: tuple[datetime.date, datetime.date] | None = None
    run_start: datetime.date | None = None
    d = start
    while d <= end + datetime.timedelta(days=1):
        missing = d <= end and d not in present
        if missing and run_start is None:
            run_start = d
        elif not missing and run_start is not None:
            run_end = d - datetime.timedelta(days=1)
            length = (run_end - run_start).days + 1
            if length >= min_days and (best is None or length > (best[1] - best[0]).days + 1):
                best = (run_start, run_end)
            run_start = None
        d += datetime.timedelta(days=1)
    return best


def comeback_end_for(return_date: datetime.date, weeks: int = COMEBACK_WEEKS) -> datetime.date:
    """The Sunday on or after *return_date* + *weeks* weeks."""
    target = return_date + datetime.timedelta(weeks=weeks)
    return target + datetime.timedelta(days=(6 - target.weekday()) % 7)


def detect_layoffs(db: Any, today: datetime.date | None = None) -> DetectedSeason:
    """Detect the injury layoff, the no-watch period, and the comeback end."""
    today = today or datetime.date.today()
    since = today - datetime.timedelta(days=365)

    placeholders = ", ".join("?" for _ in TRAINING_SPORTS)
    activity_dates = [
        r[0]
        for r in db.fetchall(
            "SELECT DISTINCT CAST(start_time AS DATE) FROM activities "
            f"WHERE sport_type IN ({placeholders}) AND start_time >= ?",
            [*TRAINING_SPORTS, since],
        )
    ]
    layoff = find_longest_layoff(activity_dates)

    # Garmin creates a daily_health row even when the watch isn't worn, so a
    # day counts as "worn" only when it has actual values.
    health_dates = [
        r[0]
        for r in db.fetchall(
            "SELECT date FROM daily_health WHERE date >= ? AND resting_hr IS NOT NULL "
            "UNION SELECT date FROM sleep_records WHERE date >= ? AND total_sleep_sec IS NOT NULL",
            [since, since],
        )
    ]
    no_device = None
    if health_dates:
        no_device = find_longest_missing_run(health_dates, min(health_dates), today)

    comeback_end = comeback_end_for(layoff.return_date) if layoff else None
    return DetectedSeason(layoff=layoff, no_device=no_device, comeback_end=comeback_end)


# ---------------------------------------------------------------------------
# Phase calendar generator
# ---------------------------------------------------------------------------

PHASE_GOALS: dict[str, str] = {
    "comeback": "Return to training after injury: consistency and tissue tolerance over intensity.",
    "base": "Aerobic base and durability: 3 load weeks + 1 recovery week.",
    "build": "Race-specific endurance: long sessions, bricks, race-pace work.",
    "peak": "Biggest race-specific sessions; hold volume, sharpen.",
    "taper": "Cut volume, keep some intensity, arrive fresh.",
    "race": "Race day.",
    "transition": "Recover physically and mentally; unstructured, easy movement.",
}


@dataclass(frozen=True)
class PhaseSpec:
    phase_type: str
    name: str
    start_date: datetime.date
    end_date: datetime.date  # inclusive
    goal: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase_type": self.phase_type,
            "name": self.name,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "goal": self.goal,
        }


def _monday(d: datetime.date) -> datetime.date:
    return d - datetime.timedelta(days=d.weekday())


def generate_phases(
    race_date: datetime.date,
    comeback_start: datetime.date | None,
    comeback_end: datetime.date | None,
    today: datetime.date,
    t: dict[str, float],
) -> list[PhaseSpec]:
    """Propose a season calendar toward *race_date* (Monday-aligned weeks).

    Base runs from the day after the comeback (or this Monday) to the start
    of build, in 4-week blocks counted backwards from build; a remainder
    shorter than a block is added to Base 1.  If there isn't room for the
    full template, base shrinks first, then build (min 6 weeks), then peak
    (min 2 weeks); taper is never shortened.
    """
    week = datetime.timedelta(weeks=1)
    day = datetime.timedelta(days=1)
    taper_weeks = int(t["phase_taper_weeks"])
    peak_weeks = int(t["phase_peak_weeks"])
    build_weeks = int(t["phase_build_weeks"])
    block_weeks = int(t["phase_base_block_weeks"])

    taper_start = _monday(race_date) - (taper_weeks - 1) * week
    if comeback_end is not None:
        season_start = comeback_end + day
    else:
        season_start = _monday(today)
    season_start = _monday(season_start + 6 * day)  # next Monday unless already Monday

    available = max(0, (taper_start - season_start).days // 7)
    if available < peak_weeks + build_weeks:
        build_weeks = max(6, available - peak_weeks)
    if available < peak_weeks + build_weeks:
        peak_weeks = max(2, available - build_weeks)
    base_weeks = max(0, available - peak_weeks - build_weeks)

    peak_start = taper_start - peak_weeks * week
    build_start = peak_start - build_weeks * week
    phases: list[PhaseSpec] = []

    if comeback_start is not None and comeback_end is not None and comeback_end >= comeback_start:
        phases.append(PhaseSpec("comeback", "Comeback", comeback_start, comeback_end, PHASE_GOALS["comeback"]))

    if base_weeks > 0:
        blocks = max(1, base_weeks // block_weeks)
        sizes = [block_weeks] * blocks
        sizes[0] += base_weeks - block_weeks * blocks  # remainder into Base 1
        if base_weeks < block_weeks:
            sizes = [base_weeks]
        start = build_start - base_weeks * week
        for i, size in enumerate(sizes, 1):
            end = start + size * week - day
            phases.append(PhaseSpec("base", f"Base {i}", start, end, PHASE_GOALS["base"]))
            start = end + day

    half = build_weeks // 2
    build_parts = (
        [(build_start, half), (build_start + half * week, build_weeks - half)] if half else [(build_start, build_weeks)]
    )
    for i, (start, size) in enumerate(build_parts, 1):
        name = f"Build {i}" if len(build_parts) > 1 else "Build"
        phases.append(PhaseSpec("build", name, start, start + size * week - day, PHASE_GOALS["build"]))

    phases.append(PhaseSpec("peak", "Peak", peak_start, taper_start - day, PHASE_GOALS["peak"]))
    phases.append(PhaseSpec("taper", "Taper", taper_start, race_date - day, PHASE_GOALS["taper"]))
    phases.append(PhaseSpec("race", "Race", race_date, race_date, PHASE_GOALS["race"]))
    transition_days = int(t["phase_transition_days"])
    phases.append(
        PhaseSpec(
            "transition", "Transition", race_date + day, race_date + transition_days * day, PHASE_GOALS["transition"]
        )
    )
    return phases


# ---------------------------------------------------------------------------
# Observed training state and phase fit flags
# ---------------------------------------------------------------------------


def _value_on_or_before(series: dict[datetime.date, float], d: datetime.date) -> float | None:
    candidates = [k for k in series if k <= d]
    return series[max(candidates)] if candidates else None


def observed_state(
    load: list[dict[str, Any]],
    training_days: set[datetime.date],
    on: datetime.date,
    t: dict[str, float],
) -> dict[str, Any]:
    """Classify the training state on *on* from the combined load curve.

    *load* rows need ``date``, ``ctl``, ``tsb``; *training_days* are dates
    with a swim/bike/run/strength session.
    """
    ctl = {r["date"]: r["ctl"] for r in load if r.get("ctl") is not None}
    tsb = {r["date"]: r["tsb"] for r in load if r.get("tsb") is not None}
    atl = {r["date"]: r["atl"] for r in load if r.get("atl") is not None}
    history_days = len([d for d in ctl if d <= on])
    ctl_now = _value_on_or_before(ctl, on)
    ctl_7 = _value_on_or_before(ctl, on - datetime.timedelta(days=7))
    ctl_28 = _value_on_or_before(ctl, on - datetime.timedelta(days=28))
    tsb_now = _value_on_or_before(tsb, on)
    consistency = len([d for d in training_days if on - datetime.timedelta(days=14) < d <= on])

    ramp = None if ctl_now is None or ctl_7 is None else round(ctl_now - ctl_7, 2)
    change = None if ctl_now is None or not ctl_28 else round(ctl_now / ctl_28 - 1, 3)
    metrics = {
        "ctl": ctl_now,
        "atl": _value_on_or_before(atl, on),
        "tsb": tsb_now,
        "ramp_7d": ramp,
        "ctl_change_28d": change,
        "consistency_14d": consistency,
    }

    if history_days < t["state_min_history_days"] or ramp is None or tsb_now is None:
        state = "insufficient_data"
    elif tsb_now < t["state_tsb_overreach"] or ramp > t["state_ramp_overreach"]:
        state = "overreaching_risk"
    elif change is not None and change < t["state_detrain_ctl_change"] and consistency < t["state_detrain_consistency"]:
        state = "detraining"
    elif ramp > t["state_building_ramp"]:
        state = "building"
    elif ramp < t["state_absorbing_ramp"] and tsb_now > t["state_absorbing_tsb"]:
        state = "absorbing"
    else:
        state = "maintaining"
    return {"state": state, **metrics}


STATE_LABELS = {
    "insufficient_data": "Not enough data",
    "overreaching_risk": "Overreaching risk",
    "detraining": "Detraining",
    "building": "Building",
    "absorbing": "Absorbing",
    "maintaining": "Maintaining",
}


def phase_flags(
    phase_type: str | None,
    state: dict[str, Any],
    recent_states: list[str],
    t: dict[str, float],
) -> list[str]:
    """Informational mismatches between the planned phase and observed load."""
    if phase_type is None:
        return ["No phase defined for today — set up your season."]
    flags: list[str] = []
    ramp = state.get("ramp_7d")
    if phase_type == "comeback" and ramp is not None and ramp > t["flag_comeback_ramp"]:
        flags.append("Ramping faster than a return-from-injury block normally allows.")
    if phase_type == "base" and state["state"] == "overreaching_risk":
        flags.append("Overreaching risk during a base phase.")
    if phase_type in ("build", "peak"):
        low = sum(1 for s in recent_states if s in ("detraining", "absorbing"))
        if low >= 10:
            flags.append(f"Load has been dropping for {low} of the last 14 days during {phase_type}.")
    if phase_type == "taper" and ramp is not None and ramp > 0:
        flags.append("Load is still rising during the taper.")
    return flags
