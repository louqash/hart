"""Bike power analytics.

Provides normalized power, intensity factor, variability index, TSS, power
balance correction for single-sided meters (e.g. Favero Assioma L), and
power-duration curve computation.
"""

from __future__ import annotations


import numpy as np


# ---------------------------------------------------------------------------
# Power balance correction
# ---------------------------------------------------------------------------


def apply_power_balance(
    power_series: list[int | float],
    balance: str = "double_left",
) -> list[float]:
    """Correct power readings from a single-sided power meter.

    Parameters
    ----------
    power_series:
        Raw power samples (watts).  For a left-only meter such as
        the Favero Assioma L, each value represents left-leg power only.
    balance:
        Correction strategy.  Currently supported:

        * ``"double_left"`` -- multiply every sample by 2 (assumes 50/50
          left-right balance).
        * ``"none"`` -- return the series unchanged (already total power).

    Returns
    -------
    list[float]
        Corrected power series.
    """
    if balance == "double_left":
        return [float(p) * 2.0 for p in power_series]
    if balance == "none":
        return [float(p) for p in power_series]
    raise ValueError(f"Unknown power balance strategy: {balance!r}")


# ---------------------------------------------------------------------------
# Normalized Power (NP)
# ---------------------------------------------------------------------------


def normalized_power(
    power_series: list[int | float],
    sample_rate_sec: int = 1,
) -> float:
    """Compute Normalized Power per Coggan's algorithm.

    Algorithm
    ---------
    1. Compute a 30-second rolling average of power, excluding zeros (coasting/
       gaps) from the rolling window.
    2. Raise each 30-sec average to the 4th power.
    3. Take the mean of these values.
    4. Take the 4th root.

    Parameters
    ----------
    power_series:
        Second-by-second (or other interval) power readings in watts.
    sample_rate_sec:
        Seconds between consecutive samples.  Default 1 Hz.

    Returns
    -------
    float
        Normalized Power in watts.  Returns 0.0 if the series is too short
        or contains no valid data.
    """
    if not power_series:
        return 0.0

    window_samples = max(1, 30 // sample_rate_sec)

    arr = np.array(power_series, dtype=np.float64)

    if len(arr) < window_samples:
        # Series shorter than one rolling window -- fall back to simple average
        # of non-zero values.
        non_zero = arr[arr > 0]
        return float(non_zero.mean()) if len(non_zero) > 0 else 0.0

    # Build rolling averages that exclude zeros.  We use a convolution-like
    # approach: for each position we take the mean of the non-zero values in the
    # trailing window.
    rolling_avgs: list[float] = []
    for i in range(window_samples - 1, len(arr)):
        window = arr[i - window_samples + 1 : i + 1]
        non_zero = window[window > 0]
        if len(non_zero) > 0:
            rolling_avgs.append(float(non_zero.mean()))

    if not rolling_avgs:
        return 0.0

    rolling = np.array(rolling_avgs, dtype=np.float64)
    mean_fourth = np.mean(rolling**4)

    return float(mean_fourth**0.25)


# ---------------------------------------------------------------------------
# Derived metrics
# ---------------------------------------------------------------------------


def intensity_factor(np_watts: float, ftp: float) -> float:
    """Compute Intensity Factor (IF) = NP / FTP.

    Parameters
    ----------
    np_watts:
        Normalized Power in watts.
    ftp:
        Functional Threshold Power in watts.

    Returns
    -------
    float
        Intensity Factor (dimensionless ratio).  Returns 0.0 if FTP <= 0.
    """
    if ftp <= 0:
        return 0.0
    return np_watts / ftp


def variability_index(np_watts: float, avg_power: float) -> float:
    """Compute Variability Index (VI) = NP / average power.

    A VI of ~1.0 indicates very steady riding (e.g. time trial).
    Higher values indicate more variable power output.

    Parameters
    ----------
    np_watts:
        Normalized Power in watts.
    avg_power:
        Arithmetic mean power in watts.

    Returns
    -------
    float
        Variability Index.  Returns 0.0 if avg_power <= 0.
    """
    if avg_power <= 0:
        return 0.0
    return np_watts / avg_power


def tss_from_power(
    duration_sec: int,
    np_watts: float,
    ftp: float,
) -> float:
    """Compute Training Stress Score from power data.

    Formula
    -------
    TSS = (duration_sec * NP * IF) / (FTP * 3600) * 100

    where IF = NP / FTP.

    Parameters
    ----------
    duration_sec:
        Total activity duration in seconds.
    np_watts:
        Normalized Power in watts.
    ftp:
        Functional Threshold Power in watts.

    Returns
    -------
    float
        Training Stress Score.  Returns 0.0 if FTP <= 0.
    """
    if ftp <= 0:
        return 0.0
    if_value = intensity_factor(np_watts, ftp)
    return (duration_sec * np_watts * if_value) / (ftp * 3600) * 100


# ---------------------------------------------------------------------------
# Power-Duration Curve
# ---------------------------------------------------------------------------

# Standard durations (in seconds) for the power-duration curve.
_PDC_DURATIONS_SEC: list[int] = [
    1,
    5,
    10,
    30,
    60,        # 1 min
    120,       # 2 min
    300,       # 5 min
    600,       # 10 min
    1200,      # 20 min
    1800,      # 30 min
    3600,      # 60 min
    5400,      # 90 min
    7200,      # 120 min
]


def power_duration_curve(
    power_series: list[int | float],
    sample_rate_sec: int = 1,
) -> dict[int, float]:
    """Compute the best average power for a set of standard durations.

    For each target duration the function finds the maximum mean power over
    any contiguous window of that length using a rolling-sum approach for
    O(n) performance per duration.

    Parameters
    ----------
    power_series:
        Second-by-second (or other interval) power readings in watts.
    sample_rate_sec:
        Seconds between consecutive samples.

    Returns
    -------
    dict[int, float]
        Mapping of duration in seconds to best average power in watts.
        Only durations that fit within the recorded data are included.
    """
    if not power_series:
        return {}

    arr = np.array(power_series, dtype=np.float64)
    n = len(arr)
    result: dict[int, float] = {}

    # Pre-compute cumulative sum for efficient windowed means.
    cumsum = np.concatenate(([0.0], np.cumsum(arr)))

    for dur_sec in _PDC_DURATIONS_SEC:
        window_samples = max(1, dur_sec // sample_rate_sec)
        if window_samples > n:
            continue  # recording too short for this duration

        # Windowed sums via cumulative sum difference.
        window_sums = cumsum[window_samples:] - cumsum[:n - window_samples + 1]
        best_avg = float(np.max(window_sums)) / window_samples
        result[dur_sec] = round(best_avg, 1)

    return result
