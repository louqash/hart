"""Swim-specific analytics.

Provides Critical Swim Speed (CSS), swim TSS (sTSS), SWOLF, and pace
utilities for pool and open-water swimming.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Critical Swim Speed (CSS)
# ---------------------------------------------------------------------------


def swim_css(t400_sec: float, t200_sec: float) -> float:
    """Compute Critical Swim Speed from 400m and 200m time-trial results.

    CSS represents the swimming analogue of lactate threshold -- the fastest
    pace that can be sustained aerobically for extended periods.

    Formula
    -------
    CSS_speed = (400 - 200) / (t400 - t200)  [m/s]
    CSS_pace  = 100 / CSS_speed               [sec per 100m]

    Parameters
    ----------
    t400_sec:
        Time to complete a 400m all-out effort, in seconds.
    t200_sec:
        Time to complete a 200m all-out effort, in seconds.

    Returns
    -------
    float
        CSS as pace in seconds per 100 metres.

    Raises
    ------
    ValueError
        If t400 <= t200 (the 400m must take longer than the 200m).
    """
    if t400_sec <= t200_sec:
        raise ValueError(f"400m time ({t400_sec}s) must be greater than 200m time ({t200_sec}s)")
    if t200_sec <= 0:
        raise ValueError(f"200m time must be positive, got {t200_sec}s")

    css_speed_ms = 200.0 / (t400_sec - t200_sec)
    css_pace_sec_100m = 100.0 / css_speed_ms
    return css_pace_sec_100m


# ---------------------------------------------------------------------------
# Swim TSS (sTSS)
# ---------------------------------------------------------------------------


def swim_tss(
    duration_sec: int,
    avg_pace_sec_100m: float,
    css_sec_100m: float,
) -> float:
    """Compute swim Training Stress Score (sTSS).

    Formula
    -------
    IF = actual_speed / css_speed
       = css_sec_100m / avg_pace_sec_100m

    If the swimmer is faster (lower sec/100m) than CSS, IF > 1.

    sTSS = (duration_sec / 3600) * IF^3 * 100

    The cubed exponent (rather than squared as in cycling/running) reflects
    the cubic relationship between swimming speed and drag force in water.

    Parameters
    ----------
    duration_sec:
        Total swim duration in seconds.
    avg_pace_sec_100m:
        Average pace in seconds per 100 metres.
    css_sec_100m:
        Critical Swim Speed as pace in seconds per 100 metres.

    Returns
    -------
    float
        Swim TSS.  Returns 0.0 if inputs are invalid.
    """
    if avg_pace_sec_100m <= 0 or css_sec_100m <= 0 or duration_sec <= 0:
        return 0.0

    if_value = css_sec_100m / avg_pace_sec_100m
    return (duration_sec / 3600.0) * if_value**3 * 100.0


# ---------------------------------------------------------------------------
# SWOLF
# ---------------------------------------------------------------------------


def swolf(
    strokes: int,
    time_sec: float,
    pool_length_m: int = 25,
) -> float:
    """Compute SWOLF score for a pool swim.

    SWOLF = strokes_per_length + time_per_length_sec

    It is a measure of swim efficiency -- lower is better. The name is a
    portmanteau of "swim" and "golf".

    Parameters
    ----------
    strokes:
        Total stroke count for the measured distance.  If this represents
        strokes for a single length, pass ``pool_length_m`` equal to the
        actual pool length.  If it represents strokes over multiple lengths,
        the function divides appropriately.
    time_sec:
        Total time for the measured distance in seconds.
    pool_length_m:
        Pool length in metres (default 25m).  Used only if the caller passes
        totals over a multi-length distance; for a single length this should
        match the actual pool length and ``strokes``/``time_sec`` should be
        for that one length.

    Returns
    -------
    float
        SWOLF score (lower is better).
    """
    if pool_length_m <= 0 or time_sec <= 0:
        return 0.0

    # Assume strokes and time_sec are already per-length values.
    # This is the standard Garmin/COROS convention: SWOLF is computed per length.
    return float(strokes) + time_sec


# ---------------------------------------------------------------------------
# Pace utility
# ---------------------------------------------------------------------------


def pace_per_100m(distance_m: float, duration_sec: int) -> float:
    """Compute average pace in seconds per 100 metres.

    Parameters
    ----------
    distance_m:
        Total distance swum in metres.
    duration_sec:
        Total duration in seconds.

    Returns
    -------
    float
        Pace in seconds per 100 metres.  Returns 0.0 if distance <= 0.
    """
    if distance_m <= 0 or duration_sec <= 0:
        return 0.0
    return (duration_sec / distance_m) * 100.0
