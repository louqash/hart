"""Aerobic efficiency metrics for tracking fitness progression.

Efficiency Factor (EF) quantifies the relationship between work output
(power or pace) and cardiac cost (heart rate).  Tracking EF over time at
a consistent aerobic intensity reveals aerobic development: a rising EF
means the athlete produces more work per heartbeat.

Aerobic decoupling (Pw:Hr or Pa:Hr) measures how much the power-to-HR or
pace-to-HR ratio drifts during the second half of a steady session.  A
well-trained aerobic base shows <5 % decoupling in zone-2 efforts.

Cardiac drift rate quantifies the linear HR rise per hour at constant power,
providing a finer-grained view of aerobic endurance.
"""

from __future__ import annotations

import numpy as np

# ---------------------------------------------------------------------------
# Efficiency Factor
# ---------------------------------------------------------------------------


def efficiency_factor_bike(normalized_power: float, avg_hr: float) -> float:
    """Compute bike Efficiency Factor: EF = NP / avg_HR.

    A higher EF indicates greater aerobic efficiency -- the athlete is
    producing more power per heartbeat.

    Parameters
    ----------
    normalized_power:
        Normalized Power in watts.
    avg_hr:
        Average heart rate in beats per minute.

    Returns
    -------
    float
        Efficiency Factor (watts per bpm).  Returns 0.0 if avg_hr <= 0.
    """
    if avg_hr <= 0:
        return 0.0
    return normalized_power / avg_hr


def efficiency_factor_run(avg_pace_sec_km: float, avg_hr: float) -> float:
    """Compute run Efficiency Factor: EF = speed (m/min) / avg_HR.

    Speed is derived from pace: speed_m_per_min = 1000 / pace * 60.

    Parameters
    ----------
    avg_pace_sec_km:
        Average pace in seconds per kilometre.
    avg_hr:
        Average heart rate in beats per minute.

    Returns
    -------
    float
        Efficiency Factor (m/min per bpm).  Returns 0.0 if either input
        is non-positive.
    """
    if avg_pace_sec_km <= 0 or avg_hr <= 0:
        return 0.0
    speed_m_per_min = 1000.0 / avg_pace_sec_km * 60.0
    return speed_m_per_min / avg_hr


# ---------------------------------------------------------------------------
# Aerobic Decoupling
# ---------------------------------------------------------------------------


def aerobic_decoupling(first_half_ef: float, second_half_ef: float) -> float:
    """Compute aerobic decoupling percentage (Pw:Hr or Pa:Hr).

    Decoupling measures how much the power-to-HR (or pace-to-HR) ratio
    deteriorates from the first to the second half of a session.

    decoupling = ((EF_first - EF_second) / EF_first) * 100

    A positive value means HR drifted up relative to output in the second
    half.  Target for well-trained aerobic athletes: < 5 %.

    Parameters
    ----------
    first_half_ef:
        Efficiency Factor for the first half of the activity.
    second_half_ef:
        Efficiency Factor for the second half of the activity.

    Returns
    -------
    float
        Decoupling as a percentage.  Returns 0.0 if first_half_ef <= 0.
    """
    if first_half_ef <= 0:
        return 0.0
    return ((first_half_ef - second_half_ef) / first_half_ef) * 100.0


def compute_decoupling_from_streams(
    hr_series: list[float],
    power_or_pace_series: list[float],
    sport_type: str,
) -> float:
    """Compute aerobic decoupling from second-by-second streams.

    The series are split in half and the Efficiency Factor is computed for
    each half.  For ``"bike"`` sport, EF = mean(power) / mean(HR); for
    ``"run"``, EF = mean(speed_m_per_min) / mean(HR) where speed is
    derived from pace (pace in sec/km -> speed = 1000/pace * 60).

    Zeros and non-positive values in either stream are filtered out before
    calculation.

    Parameters
    ----------
    hr_series:
        Heart rate samples (bpm), one per second.
    power_or_pace_series:
        For bike: power in watts.  For run: pace in seconds per kilometre.
    sport_type:
        ``"bike"`` or ``"run"``.

    Returns
    -------
    float
        Decoupling percentage.  Returns 0.0 if insufficient valid data.

    Raises
    ------
    ValueError
        If ``sport_type`` is not ``"bike"`` or ``"run"``.
    """
    if sport_type not in ("bike", "run"):
        raise ValueError(f"sport_type must be 'bike' or 'run', got {sport_type!r}")

    if len(hr_series) != len(power_or_pace_series):
        raise ValueError(
            f"hr_series length ({len(hr_series)}) != power_or_pace_series length ({len(power_or_pace_series)})"
        )

    hr = np.array(hr_series, dtype=np.float64)
    values = np.array(power_or_pace_series, dtype=np.float64)

    # Filter out zero / non-positive samples in either stream.
    valid = (hr > 0) & (values > 0)
    hr = hr[valid]
    values = values[valid]

    if len(hr) < 4:
        # Need at least 2 samples per half.
        return 0.0

    mid = len(hr) // 2

    hr_first = hr[:mid]
    hr_second = hr[mid:]
    val_first = values[:mid]
    val_second = values[mid:]

    avg_hr_first = float(np.mean(hr_first))
    avg_hr_second = float(np.mean(hr_second))

    if avg_hr_first <= 0 or avg_hr_second <= 0:
        return 0.0

    if sport_type == "bike":
        # EF = mean power / mean HR
        ef_first = float(np.mean(val_first)) / avg_hr_first
        ef_second = float(np.mean(val_second)) / avg_hr_second
    else:
        # Run: convert pace (sec/km) to speed (m/min), then EF = speed / HR
        speed_first = 1000.0 / np.mean(val_first) * 60.0
        speed_second = 1000.0 / np.mean(val_second) * 60.0
        ef_first = speed_first / avg_hr_first
        ef_second = speed_second / avg_hr_second

    return aerobic_decoupling(ef_first, ef_second)


# Session-level decoupling gates (Fitness "Staying Power", session pages, grading).
DECOUPLING_MIN_MOVING_SEC = 45 * 60
DECOUPLING_WARMUP_SEC = 10 * 60
DECOUPLING_MAX_GAP_SEC = 10  # longer gaps are pauses, not moving time
BIKE_MAX_VARIABILITY_INDEX = 1.10  # NP / avg power; intervals & outdoor rides ≥ ~1.2
RUN_MAX_SPEED_CV = 0.10  # CV of 1-min mean speed; interval runs ≥ ~0.12


def steady_session_decoupling(
    timestamps: list[float],
    hr_series: list[float | None],
    output_series: list[float | None],
    sport_type: str,
    *,
    min_moving_sec: float = DECOUPLING_MIN_MOVING_SEC,
    warmup_sec: float = DECOUPLING_WARMUP_SEC,
    require_steady: bool = True,
) -> float | None:
    """Aerobic decoupling for a whole session, or ``None`` if it doesn't qualify.

    Unlike :func:`compute_decoupling_from_streams` this works on raw, aligned
    stream rows: ``timestamps`` may be absolute (epoch seconds, as stored in
    ``activity_streams``) or relative — only differences are used.  A sample
    counts when HR and output are both positive; the output is power (W) for
    ``"bike"`` and speed (any unit) for ``"run"``.

    Gates:
      * at least ``min_moving_sec`` of moving time (gaps > 10 s don't count);
      * the first ``warmup_sec`` of moving time is dropped before splitting;
      * with ``require_steady``: bike variability index ≤ 1.10, run CV of
        1-minute mean speed ≤ 0.10 — decoupling of interval sessions is noise.

    Returns the decoupling percentage (positive = HR drifted up relative to
    output in the second half).
    """
    if sport_type not in ("bike", "run"):
        return None
    n = len(timestamps)
    if n == 0 or len(hr_series) != n or len(output_series) != n:
        return None

    t = np.array(timestamps, dtype=np.float64)
    hr = np.array([h if h is not None else np.nan for h in hr_series], dtype=np.float64)
    out = np.array([o if o is not None else np.nan for o in output_series], dtype=np.float64)
    order = np.argsort(t, kind="stable")
    t, hr, out = t[order], hr[order], out[order]

    valid = (hr > 0) & (out > 0)  # NaN compares False
    if not valid.any():
        return None
    t, hr, out = t[valid], hr[valid], out[valid]

    # Moving time: each sample's step from the previous valid sample, with
    # pauses (and the first sample) counting as one second.
    dt = np.diff(t, prepend=t[0] - 1.0)
    dt = np.where((dt <= 0) | (dt > DECOUPLING_MAX_GAP_SEC), 1.0, dt)
    moving = np.cumsum(dt)
    if moving[-1] < min_moving_sec:
        return None

    keep = moving > warmup_sec
    hr, out, moving = hr[keep], out[keep], moving[keep]
    if len(hr) < 4:
        return None

    if require_steady and not _is_steady(out, moving, sport_type):
        return None

    # Split at half of the analysed moving time, not half of the samples.
    mid_time = (moving[0] + moving[-1]) / 2.0
    first = moving <= mid_time
    second = ~first
    if first.sum() < 2 or second.sum() < 2:
        return None

    ef_first = float(np.mean(out[first])) / float(np.mean(hr[first]))
    ef_second = float(np.mean(out[second])) / float(np.mean(hr[second]))
    return aerobic_decoupling(ef_first, ef_second)


def _is_steady(out: np.ndarray, moving: np.ndarray, sport_type: str) -> bool:
    if sport_type == "bike":
        # 30-s rolling mean, 4th-power average (Coggan NP) over the analysed part.
        window = min(30, len(out))
        rolling = np.convolve(out, np.ones(window) / window, mode="valid")
        np_val = float(np.mean(rolling**4) ** 0.25)
        avg = float(np.mean(out))
        return avg > 0 and np_val / avg <= BIKE_MAX_VARIABILITY_INDEX
    minute = ((moving - moving[0]) // 60).astype(np.int64)
    sums = np.bincount(minute, weights=out)
    counts = np.bincount(minute)
    means = sums[counts > 0] / counts[counts > 0]
    if len(means) < 2 or float(np.mean(means)) <= 0:
        return False
    return float(np.std(means, ddof=1) / np.mean(means)) <= RUN_MAX_SPEED_CV


# ---------------------------------------------------------------------------
# Cardiac Drift Rate
# ---------------------------------------------------------------------------


def cardiac_drift_rate(
    hr_series: list[float],
    power_series: list[float] | None,
    duration_sec: int,
) -> float:
    """Compute the cardiac drift rate: HR increase per hour at constant effort.

    A linear regression of HR over time is performed.  If ``power_series``
    is provided, only time-points where power is within +/-10 % of the mean
    power are included (isolating steady-state effort).

    Parameters
    ----------
    hr_series:
        Second-by-second heart rate samples (bpm).
    power_series:
        Optional second-by-second power readings in watts.  When given,
        analysis is restricted to periods where power is within 10 % of
        the mean (steady-state filter).
    duration_sec:
        Total activity duration in seconds (used only for validation;
        the series length is authoritative).

    Returns
    -------
    float
        Heart rate increase per hour (bpm/hour).  A positive value means
        HR is drifting upward.  Returns 0.0 if insufficient data.
    """
    hr = np.array(hr_series, dtype=np.float64)

    if len(hr) < 10:
        return 0.0

    time_sec = np.arange(len(hr), dtype=np.float64)

    if power_series is not None and len(power_series) == len(hr):
        power = np.array(power_series, dtype=np.float64)
        # Steady-state filter: keep only samples where power is within
        # +/-10 % of the mean non-zero power.
        non_zero_power = power[power > 0]
        if len(non_zero_power) < 10:
            return 0.0
        mean_power = float(np.mean(non_zero_power))
        lower = mean_power * 0.90
        upper = mean_power * 1.10
        steady_mask = (power >= lower) & (power <= upper) & (hr > 0)
        hr = hr[steady_mask]
        time_sec = time_sec[steady_mask]

    # Filter out zero HR values.
    valid = hr > 0
    hr = hr[valid]
    time_sec = time_sec[valid]

    if len(hr) < 10:
        return 0.0

    # Simple linear regression: HR = a + b * time
    # b gives bpm per second; convert to bpm per hour.
    mean_t = np.mean(time_sec)
    mean_hr = np.mean(hr)
    numerator = np.sum((time_sec - mean_t) * (hr - mean_hr))
    denominator = np.sum((time_sec - mean_t) ** 2)

    if denominator == 0:
        return 0.0

    slope_per_sec = float(numerator / denominator)
    slope_per_hour = slope_per_sec * 3600.0

    return slope_per_hour
