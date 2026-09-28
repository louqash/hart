"""Training compliance analysis: planned vs actual.

Compares actual training execution against a prescribed plan to quantify
adherence and identify under- or over-training patterns.  Also checks
whether weekly volume progression follows expected periodisation patterns
(e.g. 3-week build + 1-week recovery).

Compliance analysis is critical for:

- **Consistency tracking**: The most trainable athletes are the most
  consistent.
- **Load management**: Detecting when actual load deviates dangerously
  from plan.
- **Periodisation adherence**: Verifying that recovery weeks actually
  happen and build phases ramp appropriately.
"""

from __future__ import annotations

import numpy as np

from hart.models.metrics import WeeklySummary


# ---------------------------------------------------------------------------
# Weekly Compliance
# ---------------------------------------------------------------------------


def weekly_compliance(
    planned: dict,
    actual: WeeklySummary,
) -> dict:
    """Compare actual weekly training to the plan.

    Parameters
    ----------
    planned:
        Planned training targets.  Expected keys (all optional):

        - ``swim_sessions``, ``swim_duration_min``, ``swim_distance_m``
        - ``bike_sessions``, ``bike_duration_min``, ``bike_distance_m``
        - ``run_sessions``, ``run_duration_min``, ``run_distance_m``
        - ``total_sessions``, ``total_duration_min``, ``total_tss``

    actual:
        Actual weekly summary (a :class:`WeeklySummary` for a specific
        sport, or an aggregated summary).

    Returns
    -------
    dict
        Keys:

        - ``session_compliance_pct``: Actual / planned sessions * 100.
        - ``duration_compliance_pct``: Actual / planned duration * 100.
        - ``distance_compliance_pct``: Actual / planned distance * 100.
        - ``tss_compliance_pct``: Actual / planned TSS * 100.
        - ``flags``: List of warning strings for significant deviations.
        - ``overall_compliance_pct``: Average of available compliance
          metrics.
    """
    sport = actual.sport_type.value if actual.sport_type else "total"

    def _pct(actual_val: float, planned_val: float | None) -> float | None:
        if planned_val is None or planned_val <= 0:
            return None
        return round((actual_val / planned_val) * 100.0, 1)

    # Map sport-specific planned keys.
    planned_sessions = planned.get(f"{sport}_sessions") or planned.get("total_sessions")
    planned_duration_min = planned.get(f"{sport}_duration_min") or planned.get("total_duration_min")
    planned_distance_m = planned.get(f"{sport}_distance_m")
    planned_tss = planned.get(f"{sport}_tss") or planned.get("total_tss")

    actual_duration_min = actual.total_duration_sec / 60.0

    session_pct = _pct(actual.session_count, planned_sessions)
    duration_pct = _pct(actual_duration_min, planned_duration_min)
    distance_pct = _pct(actual.total_distance_m, planned_distance_m)
    tss_pct = _pct(actual.total_tss, planned_tss)

    # ---- Flags ----
    flags: list[str] = []

    if session_pct is not None:
        if session_pct < 70:
            flags.append(
                f"Under-training: only {actual.session_count} of "
                f"{planned_sessions} planned {sport} sessions completed ({session_pct:.0f}%)."
            )
        elif session_pct > 130:
            flags.append(
                f"Over-training: {actual.session_count} sessions vs "
                f"{planned_sessions} planned {sport} sessions ({session_pct:.0f}%)."
            )

    if duration_pct is not None:
        if duration_pct < 70:
            flags.append(
                f"Duration deficit ({sport}): {actual_duration_min:.0f} min "
                f"of {planned_duration_min:.0f} min planned ({duration_pct:.0f}%)."
            )
        elif duration_pct > 130:
            flags.append(
                f"Duration excess ({sport}): {actual_duration_min:.0f} min "
                f"vs {planned_duration_min:.0f} min planned ({duration_pct:.0f}%)."
            )

    if tss_pct is not None:
        if tss_pct < 60:
            flags.append(
                f"TSS well below plan ({sport}): {actual.total_tss:.0f} "
                f"of {planned_tss:.0f} planned ({tss_pct:.0f}%)."
            )
        elif tss_pct > 140:
            flags.append(
                f"TSS significantly above plan ({sport}): {actual.total_tss:.0f} "
                f"vs {planned_tss:.0f} planned ({tss_pct:.0f}%). Risk of overreaching."
            )

    # ---- Overall compliance ----
    available = [v for v in [session_pct, duration_pct, distance_pct, tss_pct] if v is not None]
    overall = round(float(np.mean(available)), 1) if available else None

    return {
        "sport": sport,
        "week_start": str(actual.week_start),
        "session_compliance_pct": session_pct,
        "duration_compliance_pct": duration_pct,
        "distance_compliance_pct": distance_pct,
        "tss_compliance_pct": tss_pct,
        "overall_compliance_pct": overall,
        "flags": flags,
    }


# ---------------------------------------------------------------------------
# Volume Trend Compliance
# ---------------------------------------------------------------------------


def volume_trend_compliance(
    weekly_summaries: list[dict],
    expected_progression: str = "build",
) -> dict:
    """Check whether weekly volume follows the expected periodisation pattern.

    For a ``"build"`` pattern:

    - Weeks 1-3: volume should increase 5-10 % week-over-week.
    - Week 4: recovery week at approximately 60 % of the peak week.

    This repeats in 4-week mesocycles.

    Parameters
    ----------
    weekly_summaries:
        List of dicts with at least ``week_start`` and ``total_tss``
        (or ``total_duration_sec``), ordered chronologically.
    expected_progression:
        ``"build"`` (default) -- standard 3:1 build/recovery.

    Returns
    -------
    dict
        Keys:

        - ``pattern_match``: ``True`` if the recent volume broadly
          follows the expected pattern.
        - ``current_phase``: ``"build"`` or ``"recovery"`` based on
          the detected position in the mesocycle.
        - ``week_over_week_changes``: List of percentage changes.
        - ``notes``: List of observations / warnings.
    """
    if len(weekly_summaries) < 3:
        return {
            "pattern_match": None,
            "current_phase": "unknown",
            "week_over_week_changes": [],
            "notes": ["Insufficient data (need at least 3 weeks)."],
        }

    # Extract weekly volume (prefer TSS, fall back to duration).
    volumes: list[float] = []
    for ws in weekly_summaries:
        tss = ws.get("total_tss", 0) or 0
        if tss > 0:
            volumes.append(float(tss))
        else:
            dur = ws.get("total_duration_sec", 0) or 0
            volumes.append(float(dur) / 60.0)  # convert to minutes as proxy

    # Week-over-week changes.
    wow_changes: list[float] = []
    for i in range(1, len(volumes)):
        if volumes[i - 1] > 0:
            pct_change = ((volumes[i] - volumes[i - 1]) / volumes[i - 1]) * 100.0
        else:
            pct_change = 0.0
        wow_changes.append(round(pct_change, 1))

    notes: list[str] = []
    pattern_match = True
    current_phase = "build"

    if expected_progression == "build":
        # Analyse the most recent 4-week block (or available data).
        recent_n = min(len(volumes), 4)
        recent = volumes[-recent_n:]
        recent_changes = wow_changes[-(recent_n - 1):] if len(wow_changes) >= (recent_n - 1) else wow_changes

        # Check for recovery week pattern: last week significantly lower.
        if recent_n >= 4:
            peak_of_build = max(recent[:3])
            last_week = recent[-1]

            if peak_of_build > 0:
                recovery_ratio = last_week / peak_of_build

                if recovery_ratio < 0.75:
                    current_phase = "recovery"
                    notes.append(
                        f"Recovery week detected: volume at {recovery_ratio*100:.0f}% "
                        f"of peak build week."
                    )
                else:
                    current_phase = "build"

                # Check build weeks increased appropriately.
                build_increases = recent_changes[:2] if len(recent_changes) >= 2 else recent_changes
                for i, change in enumerate(build_increases):
                    if change < 0:
                        notes.append(
                            f"Week {i+2} volume decreased by {abs(change):.1f}% "
                            f"(expected increase during build phase)."
                        )
                        pattern_match = False
                    elif change > 15:
                        notes.append(
                            f"Week {i+2} volume jumped {change:.1f}% "
                            f"(recommended: 5-10% increase). Risk of overload."
                        )
                        pattern_match = False
            else:
                notes.append("Insufficient volume data to assess pattern.")
                pattern_match = False
        else:
            # Less than 4 weeks: just check for reasonable progression.
            for i, change in enumerate(recent_changes):
                if change < -20:
                    notes.append(
                        f"Significant volume drop in week {i+2}: {change:.1f}%."
                    )
                    pattern_match = False
                elif change > 20:
                    notes.append(
                        f"Large volume spike in week {i+2}: {change:.1f}%."
                    )
                    pattern_match = False

        # Check if a recovery week is overdue.
        if len(volumes) >= 4:
            # Look at last 4 weeks: if all are increasing, recovery may be needed.
            last_4 = volumes[-4:]
            all_increasing = all(
                last_4[i] < last_4[i + 1] for i in range(3)
            )
            if all_increasing:
                notes.append(
                    "4 consecutive build weeks without recovery. "
                    "Consider scheduling a recovery week."
                )

    if not notes:
        notes.append("Volume progression looks appropriate.")

    return {
        "pattern_match": pattern_match,
        "current_phase": current_phase,
        "week_over_week_changes": wow_changes,
        "notes": notes,
    }
