"""Run and swim pace analytics.

Provides Grade Adjusted Pace (GAP) using the Minetti cost-of-transport model,
Normalized Graded Pace (NGP), running TSS (rTSS), and pace/speed conversion
utilities.
"""

from __future__ import annotations

import numpy as np

# ---------------------------------------------------------------------------
# Minetti cost-of-transport model
# ---------------------------------------------------------------------------

# Polynomial coefficients from Minetti et al. (2002):
#   C(i) = 155.4*i^5 - 30.4*i^4 - 43.3*i^3 + 46.3*i^2 + 19.5*i + 3.6
# where i is the grade as a decimal (e.g. 0.05 for 5%).
#
# C represents the metabolic cost of transport in J/(kg*m).  The value at
# i=0 (flat) is 3.6 J/(kg*m).

_MINETTI_COEFFS: tuple[float, ...] = (155.4, -30.4, -43.3, 46.3, 19.5, 3.6)
_C_FLAT: float = 3.6  # C(0) = constant term


def _cost_of_transport(grade_decimal: float) -> float:
    """Evaluate Minetti's cost-of-transport polynomial at a given grade.

    Parameters
    ----------
    grade_decimal:
        Grade as a decimal fraction (e.g. 0.10 for 10%).

    Returns
    -------
    float
        Metabolic cost of transport in J/(kg*m).  Clamped to a minimum
        of 1.0 to avoid non-physical negative values at extreme downhills.
    """
    i = grade_decimal
    c = (
        _MINETTI_COEFFS[0] * i**5
        + _MINETTI_COEFFS[1] * i**4
        + _MINETTI_COEFFS[2] * i**3
        + _MINETTI_COEFFS[3] * i**2
        + _MINETTI_COEFFS[4] * i
        + _MINETTI_COEFFS[5]
    )
    # Clamp: at steep downhills the polynomial can go below zero, which is
    # non-physical for our purposes.
    return max(c, 1.0)


def grade_adjusted_pace(
    pace_sec_km: float,
    grade_percent: float,
) -> float:
    """Convert an actual pace on a grade to the equivalent flat-ground pace.

    Uses the Minetti (2002) cost-of-transport model.  The idea is that
    the metabolic *cost* of running at a given pace on a slope can be
    compared to the cost of running on flat ground to derive the pace you
    would sustain on flat terrain at the same effort.

    GAP = pace * C_flat / C_grade

    Running uphill (C_grade > C_flat) produces a *lower* (faster) GAP,
    reflecting that the actual pace understates the effort.  Conversely,
    running downhill yields a slower GAP.

    Parameters
    ----------
    pace_sec_km:
        Actual pace in seconds per kilometre.
    grade_percent:
        Gradient in percent (e.g. 5.0 for 5% uphill, -3.0 for 3% downhill).

    Returns
    -------
    float
        Grade-adjusted pace in seconds per kilometre.  Returns the input
        pace unchanged if grade is exactly 0.
    """
    if pace_sec_km <= 0:
        return 0.0
    grade_decimal = grade_percent / 100.0
    c_grade = _cost_of_transport(grade_decimal)
    return pace_sec_km * _C_FLAT / c_grade


# ---------------------------------------------------------------------------
# Normalized Graded Pace (NGP)
# ---------------------------------------------------------------------------


def normalized_graded_pace(
    pace_series: list[float],
    grade_series: list[float],
    sample_rate_sec: int = 1,
) -> float:
    """Compute Normalized Graded Pace for a run activity.

    Analogous to Normalized Power for cycling:

    1. Apply grade-adjusted pace (GAP) to every sample.
    2. Convert GAP values to *speed* (1000 / gap).
    3. Compute 30-second rolling averages of speed.
    4. Raise each rolling average to the 4th power.
    5. Take the mean.
    6. Take the 4th root.
    7. Convert back to pace (1000 / result_speed).

    The computation is done in the speed domain because pace is inversely
    proportional to effort (lower pace = more effort), and the 4th-power
    weighting must operate on a quantity that scales positively with effort.

    Parameters
    ----------
    pace_series:
        Second-by-second pace in seconds per kilometre.
    grade_series:
        Second-by-second gradient in percent (same length as pace_series).
    sample_rate_sec:
        Seconds between consecutive samples.

    Returns
    -------
    float
        Normalized Graded Pace in seconds per kilometre.
        Returns 0.0 if input data is insufficient.
    """
    if not pace_series or not grade_series:
        return 0.0
    if len(pace_series) != len(grade_series):
        raise ValueError(f"pace_series length ({len(pace_series)}) != grade_series length ({len(grade_series)})")

    # Step 1: compute GAP for each sample point.
    gap_values = [grade_adjusted_pace(p, g) for p, g in zip(pace_series, grade_series)]

    # Step 2: convert to speed (m/s) -- filter out zeros/invalid.
    speeds: list[float] = []
    for gap in gap_values:
        if gap > 0:
            speeds.append(1000.0 / gap)
        else:
            speeds.append(0.0)

    speed_arr = np.array(speeds, dtype=np.float64)

    window_samples = max(1, 30 // sample_rate_sec)

    if len(speed_arr) < window_samples:
        # Too short for rolling window -- use simple mean of non-zero speeds.
        non_zero = speed_arr[speed_arr > 0]
        if len(non_zero) == 0:
            return 0.0
        mean_speed = float(non_zero.mean())
        return 1000.0 / mean_speed if mean_speed > 0 else 0.0

    # Step 3: 30-sec rolling average of speed, excluding zeros.
    rolling_avgs: list[float] = []
    for i in range(window_samples - 1, len(speed_arr)):
        window = speed_arr[i - window_samples + 1 : i + 1]
        non_zero = window[window > 0]
        if len(non_zero) > 0:
            rolling_avgs.append(float(non_zero.mean()))

    if not rolling_avgs:
        return 0.0

    rolling = np.array(rolling_avgs, dtype=np.float64)

    # Steps 4-6: 4th-power averaging.
    mean_fourth = float(np.mean(rolling**4))
    ngp_speed = mean_fourth**0.25

    # Step 7: convert back to pace.
    if ngp_speed <= 0:
        return 0.0
    return 1000.0 / ngp_speed


# ---------------------------------------------------------------------------
# Running TSS (rTSS)
# ---------------------------------------------------------------------------


def run_tss(
    duration_sec: int,
    ngp_sec_km: float,
    ftp_pace_sec_km: float,
) -> float:
    """Compute running Training Stress Score (rTSS).

    Formula
    -------
    IF = FTP_speed / NGP_speed

    Since speed = 1000 / pace_sec_km:
        IF = (1000 / ftp_pace_sec_km) / (1000 / ngp_sec_km)
           = ngp_sec_km ... wait, let's be precise.

    Actually:
        IF = athlete_speed / threshold_speed
           = (1000 / ngp_sec_km) / (1000 / ftp_pace_sec_km)
           = ftp_pace_sec_km / ngp_sec_km

    If the athlete runs *faster* than threshold (lower sec/km), IF > 1.

    rTSS = (duration_sec / 3600) * IF^2 * 100

    Parameters
    ----------
    duration_sec:
        Total run duration in seconds.
    ngp_sec_km:
        Normalized Graded Pace in seconds per kilometre.
    ftp_pace_sec_km:
        Functional Threshold Pace in seconds per kilometre.

    Returns
    -------
    float
        Running TSS.  Returns 0.0 if inputs are invalid.
    """
    if ngp_sec_km <= 0 or ftp_pace_sec_km <= 0 or duration_sec <= 0:
        return 0.0

    if_value = ftp_pace_sec_km / ngp_sec_km
    return (duration_sec / 3600.0) * if_value**2 * 100.0


# ---------------------------------------------------------------------------
# Conversion utilities
# ---------------------------------------------------------------------------


def speed_to_pace(speed_ms: float) -> float:
    """Convert speed in m/s to pace in seconds per kilometre.

    Parameters
    ----------
    speed_ms:
        Speed in metres per second.

    Returns
    -------
    float
        Pace in seconds per kilometre.  Returns 0.0 if speed <= 0.
    """
    if speed_ms <= 0:
        return 0.0
    return 1000.0 / speed_ms


def pace_to_speed(pace_sec_km: float) -> float:
    """Convert pace in seconds per kilometre to speed in m/s.

    Parameters
    ----------
    pace_sec_km:
        Pace in seconds per kilometre.

    Returns
    -------
    float
        Speed in metres per second.  Returns 0.0 if pace <= 0.
    """
    if pace_sec_km <= 0:
        return 0.0
    return 1000.0 / pace_sec_km
