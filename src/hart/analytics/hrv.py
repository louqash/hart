"""Heart Rate Variability analysis from overnight and activity data.

HRV (specifically RMSSD) reflects parasympathetic nervous system activity
and is a strong marker of recovery status and autonomic readiness.  Tracking
the natural log of RMSSD (lnRMSSD) over time reveals long-term trends in
aerobic fitness and fatigue accumulation.

Key metrics:
- **RMSSD**: Root Mean Square of Successive Differences -- the gold-standard
  short-term HRV metric.
- **lnRMSSD**: Natural log transform for better statistical properties.
- **HRV CV**: Coefficient of Variation over a rolling window -- lower CV
  indicates more stable recovery.
- **Trend analysis**: Rolling means, z-scores against a 30-day baseline,
  and directional assessment.
- **Suppression detection**: Consecutive days below a personal low baseline
  signals accumulated fatigue or overreaching.
"""

from __future__ import annotations

import datetime
import math

import numpy as np

# ---------------------------------------------------------------------------
# RMSSD computation
# ---------------------------------------------------------------------------


def _filter_rr_artifacts(rr_intervals_ms: list[float]) -> list[float]:
    """Remove RR-interval artifacts using a successive-difference threshold.

    An RR interval is considered an artifact if it differs from the
    preceding interval by more than 30 % of the preceding interval.
    This is a standard ectopic-beat filter (Malik, 1996).

    Parameters
    ----------
    rr_intervals_ms:
        Raw RR intervals in milliseconds.

    Returns
    -------
    list[float]
        Cleaned RR intervals with artifacts removed.
    """
    if len(rr_intervals_ms) < 2:
        return list(rr_intervals_ms)

    cleaned: list[float] = [rr_intervals_ms[0]]
    for i in range(1, len(rr_intervals_ms)):
        prev = cleaned[-1]
        curr = rr_intervals_ms[i]
        if prev <= 0:
            # Skip non-positive values entirely.
            continue
        relative_diff = abs(curr - prev) / prev
        if relative_diff <= 0.30:
            cleaned.append(curr)
        # Otherwise: artifact -- discard this interval.

    return cleaned


def rmssd(rr_intervals_ms: list[float]) -> float:
    """Compute RMSSD from RR intervals.

    RMSSD = sqrt(mean(successive_diff^2))

    Artifacts are filtered first: RR intervals that differ from the
    previous by more than 30 % are removed (standard ectopic filter).

    Parameters
    ----------
    rr_intervals_ms:
        RR intervals in milliseconds.

    Returns
    -------
    float
        RMSSD in milliseconds.  Returns 0.0 if insufficient data
        after filtering.
    """
    cleaned = _filter_rr_artifacts(rr_intervals_ms)

    if len(cleaned) < 2:
        return 0.0

    rr = np.array(cleaned, dtype=np.float64)
    diffs = np.diff(rr)

    if len(diffs) == 0:
        return 0.0

    return float(np.sqrt(np.mean(diffs**2)))


def ln_rmssd(rr_intervals_ms: list[float]) -> float:
    """Compute the natural log of RMSSD.

    lnRMSSD is preferred for trend analysis because it is more
    normally distributed than raw RMSSD, making parametric statistics
    (means, standard deviations, z-scores) more appropriate.

    Parameters
    ----------
    rr_intervals_ms:
        RR intervals in milliseconds.

    Returns
    -------
    float
        Natural log of RMSSD.  Returns 0.0 if RMSSD is zero or
        negative (degenerate data).
    """
    rmssd_val = rmssd(rr_intervals_ms)
    if rmssd_val <= 0:
        return 0.0
    return math.log(rmssd_val)


# ---------------------------------------------------------------------------
# HRV Coefficient of Variation
# ---------------------------------------------------------------------------


def hrv_coefficient_of_variation(
    daily_hrv_values: list[float],
    window: int = 7,
) -> float:
    """Compute the coefficient of variation (CV) of HRV over a rolling window.

    CV = stdev / mean over the last ``window`` values.  A lower CV
    indicates more stable recovery and a more resilient autonomic
    nervous system.  Typical well-trained athletes show a 7-day CV
    of 5-10 %.

    Parameters
    ----------
    daily_hrv_values:
        Daily HRV values (e.g. lnRMSSD or overnight RMSSD).
    window:
        Number of most recent days to include.

    Returns
    -------
    float
        CV as a proportion (e.g. 0.08 for 8 %).  Returns 0.0 if
        insufficient data or mean is zero.
    """
    if len(daily_hrv_values) < window:
        # Use all available data if less than the window.
        values = np.array(daily_hrv_values, dtype=np.float64)
    else:
        values = np.array(daily_hrv_values[-window:], dtype=np.float64)

    if len(values) < 2:
        return 0.0

    mean_val = float(np.mean(values))
    if mean_val == 0:
        return 0.0

    std_val = float(np.std(values, ddof=1))
    return std_val / mean_val


# ---------------------------------------------------------------------------
# HRV Trend Analysis
# ---------------------------------------------------------------------------


def hrv_trend_analysis(
    dates: list[datetime.date],
    hrv_values: list[float],
    window: int = 7,
) -> dict:
    """Analyse HRV trend from daily values.

    Parameters
    ----------
    dates:
        Date for each HRV measurement (same length as ``hrv_values``).
    hrv_values:
        Daily HRV metric (e.g. lnRMSSD or overnight RMSSD).
    window:
        Rolling window size for short-term statistics.

    Returns
    -------
    dict
        Keys:

        - ``rolling_mean``: 7-day rolling mean (list, NaN-padded for
          initial elements shorter than the window).
        - ``rolling_std``: 7-day rolling standard deviation.
        - ``baseline_mean``: 30-day mean (or all available data if < 30
          days).
        - ``baseline_std``: 30-day standard deviation.
        - ``z_score``: Z-score of the most recent value vs. the 30-day
          baseline.
        - ``trend_direction``: ``"improving"``, ``"stable"``, or
          ``"declining"`` based on the comparison of the last 7-day
          mean to the 30-day baseline.
        - ``days_below_baseline``: Count of the last 7 days with HRV
          below the 30-day mean.
    """
    if len(dates) != len(hrv_values):
        raise ValueError(f"dates length ({len(dates)}) != hrv_values length ({len(hrv_values)})")

    n = len(hrv_values)
    arr = np.array(hrv_values, dtype=np.float64)

    # ---- Rolling statistics (short-term window) ----
    rolling_mean: list[float] = []
    rolling_std: list[float] = []
    for i in range(n):
        start = max(0, i - window + 1)
        segment = arr[start : i + 1]
        rolling_mean.append(float(np.mean(segment)))
        rolling_std.append(float(np.std(segment, ddof=1)) if len(segment) > 1 else 0.0)

    # ---- 30-day baseline ----
    baseline_window = min(n, 30)
    baseline = arr[-baseline_window:]
    baseline_mean = float(np.mean(baseline))
    baseline_std = float(np.std(baseline, ddof=1)) if baseline_window > 1 else 0.0

    # ---- Z-score of most recent value vs. baseline ----
    if baseline_std > 0 and n > 0:
        z = (arr[-1] - baseline_mean) / baseline_std
    else:
        z = 0.0

    # ---- Trend direction ----
    # Compare the most recent `window`-day mean to the 30-day baseline.
    recent_window = min(n, window)
    recent_mean = float(np.mean(arr[-recent_window:]))

    if baseline_std > 0:
        trend_z = (recent_mean - baseline_mean) / baseline_std
    else:
        trend_z = 0.0

    if trend_z > 0.5:
        trend_direction = "improving"
    elif trend_z < -0.5:
        trend_direction = "declining"
    else:
        trend_direction = "stable"

    # ---- Days below baseline in last 7 ----
    recent_days = arr[-recent_window:]
    days_below = int(np.sum(recent_days < baseline_mean))

    return {
        "rolling_mean": rolling_mean,
        "rolling_std": rolling_std,
        "baseline_mean": baseline_mean,
        "baseline_std": baseline_std,
        "z_score": float(z),
        "trend_direction": trend_direction,
        "days_below_baseline": days_below,
    }


# ---------------------------------------------------------------------------
# HRV Suppression Detection
# ---------------------------------------------------------------------------


def detect_hrv_suppression(
    hrv_values: list[float],
    baseline_low: float,
    consecutive_days: int = 3,
) -> bool:
    """Detect sustained HRV suppression below a personal low baseline.

    Consecutive days with HRV below ``baseline_low`` is a sign of
    accumulated fatigue, overreaching, or illness.  The Garmin-reported
    ``baseline_low`` (lower bound of the "balanced" range) is the
    recommended threshold.

    Parameters
    ----------
    hrv_values:
        Recent daily HRV values ordered chronologically (most recent last).
    baseline_low:
        Personal HRV low baseline (e.g. Garmin's baseline_low_ms).
    consecutive_days:
        Number of consecutive days below ``baseline_low`` required to
        flag suppression.

    Returns
    -------
    bool
        ``True`` if the most recent ``consecutive_days`` (or more) values
        are all below ``baseline_low``.
    """
    if len(hrv_values) < consecutive_days:
        return False

    # Check the tail of the series for a run of below-baseline values.
    recent = hrv_values[-consecutive_days:]
    return all(v < baseline_low for v in recent)
