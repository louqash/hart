"""Training stress and fitness/fatigue modelling.

Provides:

* Banister TRIMP for heart-rate-only activities.
* Performance Management Chart (PMC) modelling: CTL, ATL, TSB.
* Monotony and strain calculation for overtraining monitoring.
* Database integration for daily training-load updates.
"""

from __future__ import annotations

import datetime
import logging
import math
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HR-based TRIMP (Banister)
# ---------------------------------------------------------------------------


def hr_trimp(
    duration_sec: int,
    avg_hr: float,
    resting_hr: float,
    max_hr: float,
    sex: str = "male",
) -> float:
    """Compute Banister TRIMP from heart-rate data.

    Formula
    -------
    TRIMP = duration_min * delta_hr_ratio * exp(b * delta_hr_ratio)

    where:
        delta_hr_ratio = (avg_hr - resting_hr) / (max_hr - resting_hr)
        b = 1.92 (male) or 1.67 (female)

    This exponential weighting ensures that time spent at high HR zones
    contributes disproportionately more stress than easy effort.

    Parameters
    ----------
    duration_sec:
        Activity duration in seconds.
    avg_hr:
        Average heart rate during the activity (bpm).
    resting_hr:
        Athlete's resting heart rate (bpm).
    max_hr:
        Athlete's maximum heart rate (bpm).
    sex:
        ``"male"`` or ``"female"`` -- affects the exponential weighting
        coefficient (b).

    Returns
    -------
    float
        TRIMP value.  Returns 0.0 if inputs are invalid.
    """
    if max_hr <= resting_hr:
        return 0.0
    if avg_hr <= resting_hr:
        return 0.0
    if duration_sec <= 0:
        return 0.0

    duration_min = duration_sec / 60.0
    delta_hr_ratio = (avg_hr - resting_hr) / (max_hr - resting_hr)

    # Clamp to [0, 1] to handle any edge cases.
    delta_hr_ratio = max(0.0, min(1.0, delta_hr_ratio))

    b = 1.92 if sex == "male" else 1.67
    return duration_min * delta_hr_ratio * math.exp(b * delta_hr_ratio)


# ---------------------------------------------------------------------------
# Performance Management Chart: CTL / ATL / TSB
# ---------------------------------------------------------------------------


def compute_ctl_atl_tsb(
    daily_tss: list[tuple[datetime.date, float]],
    ctl_tc: int = 42,
    atl_tc: int = 7,
) -> list[dict[str, Any]]:
    """Compute Chronic Training Load, Acute Training Load, and Training
    Stress Balance using exponentially weighted moving averages.

    Formula
    -------
    alpha_ctl = 1 - exp(-1 / ctl_tc)
    alpha_atl = 1 - exp(-1 / atl_tc)

    CTL_today = CTL_yesterday + (TSS_today - CTL_yesterday) * alpha_ctl
    ATL_today = ATL_yesterday + (TSS_today - ATL_yesterday) * alpha_atl
    TSB_today = CTL_today - ATL_today

    The exact decay factor ``1 - exp(-1/tc)`` is used rather than the
    simplified ``1/tc`` approximation, for mathematical accuracy.

    Parameters
    ----------
    daily_tss:
        List of (date, tss) tuples sorted by date in ascending order.
        Dates with no training should still be present with TSS = 0.
        If there are gaps, intermediate days are filled with TSS = 0.
    ctl_tc:
        Time constant for CTL in days (default 42 -- ~6 weeks).
    atl_tc:
        Time constant for ATL in days (default 7 -- ~1 week).

    Returns
    -------
    list[dict]
        One dict per day with keys: ``date``, ``daily_tss``, ``ctl``,
        ``atl``, ``tsb``.  Sorted chronologically.
    """
    if not daily_tss:
        return []

    # Sort input by date and fill gaps.
    sorted_input = sorted(daily_tss, key=lambda x: x[0])
    tss_by_date: dict[datetime.date, float] = {d: t for d, t in sorted_input}

    start_date = sorted_input[0][0]
    end_date = sorted_input[-1][0]

    alpha_ctl = 1.0 - math.exp(-1.0 / ctl_tc)
    alpha_atl = 1.0 - math.exp(-1.0 / atl_tc)

    results: list[dict[str, Any]] = []
    ctl = 0.0
    atl = 0.0

    current_date = start_date
    one_day = datetime.timedelta(days=1)

    while current_date <= end_date:
        tss = tss_by_date.get(current_date, 0.0)

        ctl = ctl + (tss - ctl) * alpha_ctl
        atl = atl + (tss - atl) * alpha_atl
        tsb = ctl - atl

        results.append(
            {
                "date": current_date,
                "daily_tss": round(tss, 1),
                "ctl": round(ctl, 2),
                "atl": round(atl, 2),
                "tsb": round(tsb, 2),
            }
        )

        current_date += one_day

    return results


# ---------------------------------------------------------------------------
# Monotony & Strain
# ---------------------------------------------------------------------------


def compute_monotony_strain(
    daily_tss_7days: list[float],
) -> tuple[float, float]:
    """Compute training monotony and strain for a 7-day block.

    Foster's monotony/strain model identifies overtraining risk:

    * **Monotony** = mean(daily_TSS) / stdev(daily_TSS).
      High monotony (> 2.0) combined with high strain signals overtraining
      risk even when total load is moderate.

    * **Strain** = sum(daily_TSS) * monotony.

    Parameters
    ----------
    daily_tss_7days:
        TSS values for 7 consecutive days.  If fewer than 7 values are
        supplied, the computation proceeds with what is available.

    Returns
    -------
    tuple[float, float]
        A 2-tuple of (monotony, strain).  Monotony is 0.0 if the standard
        deviation is 0 (i.e. all days are identical -- paradoxically, this
        is the theoretical maximum monotony, but we return 0 to avoid
        division by zero and because identical-zero training has no load).
    """
    if not daily_tss_7days:
        return 0.0, 0.0

    arr = np.array(daily_tss_7days, dtype=np.float64)
    mean_tss = float(np.mean(arr))
    std_tss = float(np.std(arr, ddof=0))  # population std (Foster uses N, not N-1)

    if std_tss <= 0:
        return 0.0, 0.0

    monotony = mean_tss / std_tss
    strain = float(np.sum(arr)) * monotony

    return round(monotony, 3), round(strain, 1)


# ---------------------------------------------------------------------------
# Database integration: update_training_load
# ---------------------------------------------------------------------------


def update_training_load(
    db: Any,
    start_date: datetime.date | None = None,
    ctl_tc: int = 42,
    atl_tc: int = 7,
) -> None:
    """Read activity metrics, compute CTL/ATL/TSB, and write to the
    ``daily_training_load`` table.

    Parameters
    ----------
    db:
        A :class:`hart.storage.Database` instance (or compatible).
    start_date:
        If given, only recompute from this date forward.  The prior day's
        CTL/ATL values are read from the database as seeds.  If ``None``,
        the full history is recomputed from scratch.
    ctl_tc:
        Chronic training load time constant (days).
    atl_tc:
        Acute training load time constant (days).
    """
    # 1. Load TSS from activity_metrics, falling back to garmin_training_load.
    if start_date:
        rows = db.fetchall(
            "SELECT CAST(a.start_time AS DATE) AS date, a.sport_type, "
            "COALESCE(m.tss, a.garmin_training_load, 0) AS tss "
            "FROM activities a "
            "LEFT JOIN activity_metrics m ON a.activity_id = m.activity_id "
            "WHERE CAST(a.start_time AS DATE) >= ? "
            "ORDER BY date",
            [start_date],
        )
    else:
        rows = db.fetchall(
            "SELECT CAST(a.start_time AS DATE) AS date, a.sport_type, "
            "COALESCE(m.tss, a.garmin_training_load, 0) AS tss "
            "FROM activities a "
            "LEFT JOIN activity_metrics m ON a.activity_id = m.activity_id "
            "ORDER BY date"
        )

    if not rows:
        logger.info("No activity metrics found; nothing to update.")
        return

    # 2. Group TSS by (date, sport_type) and also compute combined.
    tss_by_date_sport: dict[tuple[datetime.date, str], float] = {}
    tss_by_date_combined: dict[datetime.date, float] = {}

    for row_date, sport_type, tss in rows:
        if tss is None:
            continue
        d = row_date if isinstance(row_date, datetime.date) else datetime.date.fromisoformat(str(row_date))
        key = (d, sport_type)
        tss_by_date_sport[key] = tss_by_date_sport.get(key, 0.0) + tss
        tss_by_date_combined[d] = tss_by_date_combined.get(d, 0.0) + tss

    # 3. Determine all sport types present.
    sport_types = sorted({s for _, s in tss_by_date_sport})

    # 4. For each sport type (+ "combined"), compute CTL/ATL/TSB.
    all_results: list[tuple[datetime.date, str, float, float, float, float, float, float]] = []

    for sport_label in [*sport_types, "combined"]:
        if sport_label == "combined":
            daily_tss_map = tss_by_date_combined
        else:
            daily_tss_map = {d: t for (d, s), t in tss_by_date_sport.items() if s == sport_label}

        if not daily_tss_map:
            continue

        all_dates = sorted(daily_tss_map.keys())
        first_date = all_dates[0]
        last_date = all_dates[-1]

        # Build daily_tss list filling gaps with 0.
        daily_tss_list: list[tuple[datetime.date, float]] = []
        current = first_date
        one_day = datetime.timedelta(days=1)

        # Seed CTL/ATL from prior day if doing incremental update.
        seed_ctl = 0.0
        seed_atl = 0.0
        if start_date:
            prior_day = start_date - one_day
            seed_row = db.fetchone(
                "SELECT ctl, atl FROM daily_training_load WHERE date = ? AND sport_type = ?",
                [prior_day, sport_label],
            )
            if seed_row:
                seed_ctl = float(seed_row[0] or 0.0)
                seed_atl = float(seed_row[1] or 0.0)
            # Start from start_date, not from earliest activity metric.
            current = start_date
            first_date = start_date

        while current <= last_date:
            tss = daily_tss_map.get(current, 0.0)
            daily_tss_list.append((current, tss))
            current += one_day

        # Compute PMC.
        alpha_ctl = 1.0 - math.exp(-1.0 / ctl_tc)
        alpha_atl = 1.0 - math.exp(-1.0 / atl_tc)

        ctl = seed_ctl
        atl = seed_atl

        for d, tss in daily_tss_list:
            ctl = ctl + (tss - ctl) * alpha_ctl
            atl = atl + (tss - atl) * alpha_atl
            tsb = ctl - atl

            # Compute 7-day monotony & strain.
            idx = (d - first_date).days
            start_idx = max(0, idx - 6)
            week_tss = [t for _, t in daily_tss_list[start_idx : idx + 1]]
            monotony, strain = compute_monotony_strain(week_tss)

            all_results.append(
                (
                    d,
                    sport_label,
                    round(tss, 1),
                    round(ctl, 2),
                    round(atl, 2),
                    round(tsb, 2),
                    round(monotony, 3),
                    round(strain, 1),
                )
            )

    if not all_results:
        return

    # 5. Upsert into daily_training_load.
    #    DuckDB supports INSERT OR REPLACE via DELETE + INSERT pattern.
    db.executemany(
        "DELETE FROM daily_training_load WHERE date = ? AND sport_type = ?",
        [[d, sport] for d, sport, *_ in all_results],
    )

    db.executemany(
        "INSERT INTO daily_training_load "
        "(date, sport_type, daily_tss, ctl, atl, tsb, monotony, strain) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [list(r) for r in all_results],
    )

    logger.info(
        "Updated daily_training_load: %d rows for %d sport categories.",
        len(all_results),
        len(sport_types) + 1,
    )
