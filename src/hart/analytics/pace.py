"""Run and swim pace analytics.

Provides Grade Adjusted Pace (GAP) using the Minetti cost-of-transport model,
Normalized Graded Pace (NGP), the grade profile of a recorded run (from its
altitude and distance streams), running TSS (rTSS), and pace/speed conversion
utilities.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

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
# Grade from a recorded run (altitude + distance streams)
# ---------------------------------------------------------------------------

GRADE_HALF_WINDOW_M = 50.0  # grade = rise over ±50 m of distance, which smooths altimeter noise
GRADE_LIMIT_PCT = 25.0  # steeper is an altitude glitch (or hiking), not running
MIN_GRADED_DISTANCE_M = 400.0
MIN_LAP_GRADED_M = 20.0


def _cost_ratio(grade_pct: np.ndarray) -> np.ndarray:
    """Minetti cost of transport at each grade relative to the flat (1.2 = 20% more work per metre)."""
    return np.maximum(np.polyval(_MINETTI_COEFFS, grade_pct / 100.0), 1.0) / _C_FLAT


@dataclass(frozen=True)
class RunGrades:
    """A run's grade profile, one entry per stream sample that had both distance and altitude.

    ``increment_m`` is the distance since the previous sample: a recording that starts again at 0
    (a session joined from parts) or a GPS step back counts as nothing, so ``distance_m`` keeps rising.
    """

    distance_m: np.ndarray
    increment_m: np.ndarray
    grade_pct: np.ndarray

    @property
    def total_m(self) -> float:
        return float(self.increment_m.sum())

    def effort_factor(self, mask: np.ndarray | None = None) -> float | None:
        """Work of the distance covered relative to the same distance on the flat (Minetti).

        1.04 means the hills made it 4% more work than the flat, so the grade-adjusted pace is 4%
        faster than the actual one (GAP = pace / factor) and grade-adjusted speed 4% higher.
        """
        inc = self.increment_m if mask is None else self.increment_m[mask]
        grade = self.grade_pct if mask is None else self.grade_pct[mask]
        total = float(inc.sum())
        if total <= 0:
            return None
        return float((inc * _cost_ratio(grade)).sum()) / total

    def gain_loss(self, mask: np.ndarray | None = None) -> tuple[float, float]:
        """Metres climbed and descended on the smoothed profile."""
        rise = self.increment_m * self.grade_pct / 100.0
        if mask is not None:
            rise = rise[mask]
        return float(rise[rise > 0].sum()), abs(float(rise[rise < 0].sum()))

    def laps(self, lap_distances: list[float | None]) -> list[dict[str, float] | None]:
        """Effort factor, climb and descent for each lap, the laps placed on the run by their distances.

        The lap distances are scaled to the stream's total so a small difference between the two doesn't
        pile up on the last lap. None for a lap that covered (almost) no distance.
        """
        dists = np.array([d or 0.0 for d in lap_distances], dtype=np.float64)
        if not len(dists) or dists.sum() <= 0:
            return [None] * len(lap_distances)
        edges = np.cumsum(dists) * (self.total_m / dists.sum())
        which = np.minimum(np.searchsorted(edges, self.distance_m, side="left"), len(dists) - 1)
        out: list[dict[str, float] | None] = []
        for i in range(len(dists)):
            mask = which == i
            if float(self.increment_m[mask].sum()) < MIN_LAP_GRADED_M:
                out.append(None)
                continue
            gain, loss = self.gain_loss(mask)
            out.append({"effort_factor": self.effort_factor(mask) or 1.0, "gain_m": gain, "loss_m": loss})
        return out


def run_grades(
    distance: Sequence[float | None],
    altitude: Sequence[float | None],
    *,
    half_window_m: float = GRADE_HALF_WINDOW_M,
    limit_pct: float = GRADE_LIMIT_PCT,
) -> RunGrades | None:
    """Smoothed grade at every sample from a run's distance (m, cumulative) and altitude (m) streams.

    The grade at a sample is the altitude change between the points ``half_window_m`` before and after
    it on the distance axis (fewer at the ends), clamped to ±``limit_pct``. Samples without distance or
    altitude are left out. None when the samples cover less than ``MIN_GRADED_DISTANCE_M``.
    """
    pairs = [(d, a) for d, a in zip(distance, altitude, strict=True) if d is not None and a is not None]
    if len(pairs) < 2:
        return None
    raw = np.array([p[0] for p in pairs], dtype=np.float64)
    alt = np.array([p[1] for p in pairs], dtype=np.float64)
    step = np.diff(raw, prepend=raw[0])
    inc = np.where(np.isfinite(step) & (step > 0), step, 0.0)
    cum = np.cumsum(inc)
    if cum[-1] < MIN_GRADED_DISTANCE_M:
        return None
    # One altitude per distance: samples recorded while standing share a distance, so average them.
    xs, first = np.unique(cum, return_index=True)
    counts = np.diff(np.append(first, len(cum)))
    ys = np.add.reduceat(alt, first) / counts
    lo = np.maximum(cum - half_window_m, xs[0])
    hi = np.minimum(cum + half_window_m, xs[-1])
    span = hi - lo
    rise = np.interp(hi, xs, ys) - np.interp(lo, xs, ys)
    grade = np.divide(rise, span, out=np.zeros_like(rise), where=span > 0) * 100.0
    return RunGrades(distance_m=cum, increment_m=inc, grade_pct=np.clip(grade, -limit_pct, limit_pct))


def grade_adjusted_run(
    timestamps: Sequence[float],
    distance: Sequence[float | None],
    altitude: Sequence[float | None],
    speed_kmh: Sequence[float | None],
) -> dict[str, float] | None:
    """Whole-run grade adjustment from aligned stream columns (speed in km/h, as stored).

    Returns ``effort_factor`` (see :meth:`RunGrades.effort_factor`; divide a pace by it to get the
    grade-adjusted pace, multiply a speed-based efficiency by it), ``ngp_sec_km`` (Normalized Graded
    Pace, 0 when there is no speed) and the smoothed ``gain_m`` / ``loss_m``. None without enough
    distance and altitude.
    """
    keep = [i for i, (d, a) in enumerate(zip(distance, altitude, strict=True)) if d is not None and a is not None]
    grades = run_grades([distance[i] for i in keep], [altitude[i] for i in keep])
    if grades is None:
        return None
    factor = grades.effort_factor()
    if factor is None:
        return None
    pace = [3600.0 / s if s else 0.0 for s in (speed_kmh[i] for i in keep)]
    ts = np.array([timestamps[i] for i in keep], dtype=np.float64)
    rate = max(1, round(float(np.median(np.diff(ts))))) if len(ts) > 1 else 1
    gain, loss = grades.gain_loss()
    return {
        "effort_factor": factor,
        "ngp_sec_km": normalized_graded_pace(pace, [float(g) for g in grades.grade_pct], sample_rate_sec=rate),
        "gain_m": gain,
        "loss_m": loss,
    }


def grade_adjusted_speeds(
    distance: Sequence[float | None],
    altitude: Sequence[float | None],
    speed: Sequence[float | None],
) -> list[float | None]:
    """Each sample's speed (any unit) at the same effort on the flat: speed × Minetti cost ratio.

    Samples without distance or altitude, and runs too short to grade, keep their speed.
    """
    keep = [i for i, (d, a) in enumerate(zip(distance, altitude, strict=True)) if d is not None and a is not None]
    grades = run_grades([distance[i] for i in keep], [altitude[i] for i in keep])
    out = list(speed)
    if grades is None:
        return out
    for i, ratio in zip(keep, _cost_ratio(grades.grade_pct), strict=True):
        if out[i] is not None:
            out[i] = out[i] * float(ratio)
    return out


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
