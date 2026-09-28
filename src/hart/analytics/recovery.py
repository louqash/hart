"""Composite recovery score from multiple data sources.

The recovery model synthesises HRV, sleep, Garmin Body Battery, training
readiness, stress, and training-load balance (TSB) into a single 0-100
score.  Each component is normalised to 0-100 and weighted according to
its predictive value for readiness (configurable via
``RecoveryWeightsConfig``).

The default weight allocation reflects sport-science consensus on the
relative importance of each signal:

- **HRV (0.30)**: Strongest objective marker of autonomic recovery.
- **Sleep (0.25)**: Sleep quality and duration are critical for adaptation.
- **Body Battery (0.20)**: Garmin's proprietary stress/recovery model
  provides a useful composite signal.
- **Training Readiness (0.15)**: Garmin's multi-factor readiness estimate.
- **Stress (0.05)**: Inversely related to recovery.
- **Fatigue/TSB (0.05)**: Training Stress Balance captures training load
  context.
"""

from __future__ import annotations

import datetime

import numpy as np

from hart.models.metrics import RecoveryScore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    """Clamp a numeric value to [lo, hi]."""
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Component scoring
# ---------------------------------------------------------------------------


def _score_hrv(hrv: dict) -> float:
    """Score the HRV component (0-100).

    If ``baseline_low`` and ``baseline_high`` are available, the score
    is a linear interpolation between those bounds.  Otherwise a
    categorical mapping from ``hrv_status`` is used.
    """
    baseline_low = hrv.get("baseline_low")
    baseline_high = hrv.get("baseline_high")
    hrv_last_night = hrv.get("hrv_last_night")

    if baseline_low is not None and baseline_high is not None and hrv_last_night is not None:
        if baseline_high <= baseline_low:
            return 50.0
        raw = (hrv_last_night - baseline_low) / (baseline_high - baseline_low) * 100.0
        return _clamp(raw)

    # Fallback: categorical mapping
    status = hrv.get("hrv_status", "balanced")
    mapping = {
        "low": 25.0,
        "balanced": 65.0,
        "high": 90.0,
    }
    return mapping.get(str(status).lower(), 50.0)


def _score_sleep(sleep: dict) -> float:
    """Score the sleep component (0-100).

    50 % duration score (vs. 7.5 h target) + 50 % quality score.
    """
    target_sleep_sec = 7.5 * 3600.0

    total_sleep_sec = sleep.get("total_sleep_sec")
    sleep_score = sleep.get("sleep_score")

    duration_score = 50.0  # default if missing
    if total_sleep_sec is not None:
        duration_score = _clamp(total_sleep_sec / target_sleep_sec * 100.0)

    quality_score = 50.0  # default if missing
    if sleep_score is not None:
        quality_score = _clamp(float(sleep_score))

    return 0.5 * duration_score + 0.5 * quality_score


def _score_body_battery(health: dict) -> float:
    """Score the Body Battery component (0-100).

    Uses the morning (start-of-day) Body Battery value, which is
    already on a 0-100 scale from Garmin.
    """
    bb_start = health.get("body_battery_start")
    if bb_start is not None:
        return _clamp(float(bb_start))
    return 50.0  # neutral default


def _score_readiness(health: dict) -> float:
    """Score the Training Readiness component (0-100).

    Already on a 0-100 scale from Garmin.
    """
    readiness = health.get("training_readiness")
    if readiness is not None:
        return _clamp(float(readiness))
    return 50.0


def _score_stress(health: dict) -> float:
    """Score the stress component (0-100, inverted).

    Garmin average stress is 0-100 (higher = more stressed), so the
    recovery score is 100 - stress.
    """
    avg_stress = health.get("avg_stress")
    if avg_stress is not None:
        return _clamp(100.0 - float(avg_stress))
    return 50.0


def _score_fatigue(training_load: dict) -> float:
    """Score the fatigue / TSB component (0-100).

    Positive TSB (fresh) maps above 50; negative TSB (fatigued) maps
    below 50.  score = clamp(50 + TSB, 0, 100).
    """
    tsb = training_load.get("tsb")
    if tsb is not None:
        return _clamp(50.0 + float(tsb))
    return 50.0


# ---------------------------------------------------------------------------
# Composite Recovery Score
# ---------------------------------------------------------------------------


_DEFAULT_WEIGHTS: dict[str, float] = {
    "hrv": 0.30,
    "sleep": 0.25,
    "body_battery": 0.20,
    "readiness": 0.15,
    "stress": 0.05,
    "fatigue": 0.05,
}


def compute_recovery_score(
    health: dict,
    sleep: dict,
    hrv: dict,
    training_load: dict,
    weights: dict | None = None,
) -> RecoveryScore:
    """Compute a composite recovery score (0-100) from multiple data sources.

    Parameters
    ----------
    health:
        Daily health metrics.  Expected keys: ``body_battery_start``,
        ``training_readiness``, ``avg_stress``.
    sleep:
        Sleep data.  Expected keys: ``total_sleep_sec``, ``sleep_score``.
    hrv:
        HRV data.  Expected keys: ``hrv_last_night``, ``baseline_low``,
        ``baseline_high`` (or ``hrv_status`` as fallback).
    training_load:
        Training load context.  Expected keys: ``tsb``.
    weights:
        Optional override of component weights.  Keys: ``hrv``, ``sleep``,
        ``body_battery``, ``readiness``, ``stress``, ``fatigue``.
        Must sum to ~1.0.  Defaults to the standard allocation.

    Returns
    -------
    RecoveryScore
        Pydantic model with the composite score and per-component values.
    """
    w = {**_DEFAULT_WEIGHTS, **(weights or {})}

    hrv_score = _score_hrv(hrv)
    sleep_score = _score_sleep(sleep)
    bb_score = _score_body_battery(health)
    readiness_score = _score_readiness(health)
    stress_score = _score_stress(health)
    fatigue_score = _score_fatigue(training_load)

    composite = (
        hrv_score * w["hrv"]
        + sleep_score * w["sleep"]
        + bb_score * w["body_battery"]
        + readiness_score * w["readiness"]
        + stress_score * w["stress"]
        + fatigue_score * w["fatigue"]
    )
    composite = _clamp(composite)

    # Generate notes: flag components below 40 as concerning.
    notes_parts: list[str] = []
    component_labels = {
        "HRV": hrv_score,
        "Sleep": sleep_score,
        "Body Battery": bb_score,
        "Readiness": readiness_score,
        "Stress": stress_score,
        "Fatigue/TSB": fatigue_score,
    }
    for label, score in component_labels.items():
        if score < 40.0:
            notes_parts.append(f"{label} low ({score:.0f}/100)")

    notes = "; ".join(notes_parts) if notes_parts else None

    today = health.get("date") or sleep.get("date") or datetime.date.today()
    if isinstance(today, str):
        today = datetime.date.fromisoformat(today)

    return RecoveryScore(
        date=today,
        recovery_score=round(composite, 1),
        hrv_component=round(hrv_score, 1),
        sleep_component=round(sleep_score, 1),
        body_battery_component=round(bb_score, 1),
        readiness_component=round(readiness_score, 1),
        stress_component=round(stress_score, 1),
        fatigue_component=round(fatigue_score, 1),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Recovery Trend
# ---------------------------------------------------------------------------


def recovery_trend(
    scores: list[dict],
    window: int = 7,
) -> dict:
    """Analyse the recovery score trend over recent days.

    Parameters
    ----------
    scores:
        List of dicts with at least ``date`` and ``recovery_score`` keys,
        ordered chronologically (oldest first).
    window:
        Rolling window for trend assessment.

    Returns
    -------
    dict
        Keys:

        - ``rolling_mean``: Rolling mean of recovery scores.
        - ``direction``: ``"improving"``, ``"declining"``, or ``"stable"``.
        - ``days_below_60``: Count of the last ``window`` days with
          recovery_score < 60.
    """
    if not scores:
        return {
            "rolling_mean": [],
            "direction": "stable",
            "days_below_60": 0,
        }

    values = np.array(
        [s["recovery_score"] for s in scores], dtype=np.float64
    )
    n = len(values)

    # Rolling mean
    rolling_mean: list[float] = []
    for i in range(n):
        start = max(0, i - window + 1)
        rolling_mean.append(float(np.mean(values[start : i + 1])))

    # Direction: compare last `window` mean to previous `window` mean.
    recent_window = min(n, window)
    recent_mean = float(np.mean(values[-recent_window:]))

    if n > window:
        prev_mean = float(np.mean(values[-2 * window : -window]))
    else:
        prev_mean = recent_mean

    diff = recent_mean - prev_mean
    if diff > 3.0:
        direction = "improving"
    elif diff < -3.0:
        direction = "declining"
    else:
        direction = "stable"

    # Days below 60 in the recent window.
    recent_vals = values[-recent_window:]
    days_below_60 = int(np.sum(recent_vals < 60.0))

    return {
        "rolling_mean": rolling_mean,
        "direction": direction,
        "days_below_60": days_below_60,
    }
