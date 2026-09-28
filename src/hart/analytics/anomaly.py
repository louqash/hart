"""Statistical anomaly detection for overtraining and performance issues.

This module runs a battery of evidence-based checks against training load,
heart rate, performance, and recovery data to surface warning signs of
overtraining syndrome (OTS), non-functional overreaching (NFOR), illness,
and performance decline.

Detection thresholds are drawn from sport-science literature:

- **TSB thresholds**: Banister impulse-response model; TSB < -30 indicates
  deep fatigue (Busso, 2003).
- **Ramp rate**: CTL increase > 7 TSS/week risks injury/overreaching
  (Gabbett, 2016).
- **Monotony & Strain**: Foster (1998) monotony > 2.0 and high strain
  predict illness.
- **HR anomalies**: Elevated resting HR > 5 bpm above baseline is a
  classic overtraining marker (Urhausen & Kindermann, 2002).
- **Aerobic decoupling**: > 10 % in zone-2 suggests inadequate aerobic
  base (Friel, 2009).
"""

from __future__ import annotations

import datetime
import uuid

import numpy as np

from hart.models.activity import SportType
from hart.models.metrics import Anomaly, AnomalySeverity
from hart.storage.database import Database


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def z_score(value: float, series: list[float]) -> float:
    """Compute the standard z-score of *value* relative to *series*.

    z = (value - mean) / std

    Parameters
    ----------
    value:
        The observation to score.
    series:
        Reference distribution (at least 2 values for meaningful std).

    Returns
    -------
    float
        Z-score.  Returns 0.0 if the standard deviation is zero or
        the series has fewer than 2 elements.
    """
    if len(series) < 2:
        return 0.0
    arr = np.array(series, dtype=np.float64)
    std = float(np.std(arr, ddof=1))
    if std == 0:
        return 0.0
    return (value - float(np.mean(arr))) / std


def _make_anomaly(
    anomaly_type: str,
    severity: AnomalySeverity,
    metric_name: str,
    description: str,
    sport_type: SportType | None = None,
    expected_value: float | None = None,
    actual_value: float | None = None,
    z_score_val: float | None = None,
    activity_id: str | None = None,
    date_range_start: datetime.date | None = None,
    date_range_end: datetime.date | None = None,
) -> Anomaly:
    """Factory for creating Anomaly model instances."""
    return Anomaly(
        id=str(uuid.uuid4()),
        detected_at=datetime.datetime.now(tz=datetime.timezone.utc),
        anomaly_type=anomaly_type,
        severity=severity,
        sport_type=sport_type,
        metric_name=metric_name,
        expected_value=expected_value,
        actual_value=actual_value,
        z_score=z_score_val,
        description=description,
        activity_id=activity_id,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def detect_anomalies(db: Database, lookback_days: int = 90) -> list[Anomaly]:
    """Run all anomaly checks against the database.

    Queries the database for recent training load, activity, recovery,
    and health data, then delegates to the specialised check functions.

    Parameters
    ----------
    db:
        Database connection.
    lookback_days:
        Number of days of history to analyse.

    Returns
    -------
    list[Anomaly]
        All detected anomalies, sorted by severity (critical first).
    """
    cutoff = datetime.date.today() - datetime.timedelta(days=lookback_days)

    anomalies: list[Anomaly] = []

    # ---- Training load data ----
    ctl_rows = db.fetchall(
        "SELECT date, sport_type, daily_tss, ctl, atl, tsb, monotony, strain "
        "FROM daily_training_load WHERE date >= ? AND sport_type = 'combined' ORDER BY date",
        [str(cutoff)],
    )
    ctl_series = [
        {
            "date": row[0],
            "sport_type": row[1],
            "daily_tss": row[2],
            "ctl": row[3],
            "atl": row[4],
            "tsb": row[5],
            "monotony": row[6],
            "strain": row[7],
        }
        for row in ctl_rows
    ]

    atl_series = ctl_series  # same data, different perspective
    anomalies.extend(check_overtraining_risk(ctl_series, atl_series))

    # ---- Recent activities ----
    activity_rows = db.fetchall(
        "SELECT activity_id, sport_type, date, tss, "
        "efficiency_factor, aerobic_decoupling_pct "
        "FROM activity_metrics WHERE date >= ? ORDER BY date",
        [str(cutoff)],
    )
    recent_activities = [
        {
            "activity_id": row[0],
            "sport_type": row[1],
            "date": row[2],
            "tss": row[3],
            "efficiency_factor": row[4],
            "aerobic_decoupling_pct": row[5],
        }
        for row in activity_rows
    ]
    anomalies.extend(check_hr_anomalies(recent_activities))
    anomalies.extend(check_performance_decline(recent_activities, "bike"))
    anomalies.extend(check_performance_decline(recent_activities, "run"))

    # ---- Recovery & health data ----
    recovery_rows = db.fetchall(
        "SELECT date, recovery_score, hrv_component, sleep_component, "
        "body_battery_component "
        "FROM daily_recovery WHERE date >= ? ORDER BY date",
        [str(cutoff)],
    )
    recovery_scores = [
        {
            "date": row[0],
            "recovery_score": row[1],
            "hrv_component": row[2],
            "sleep_component": row[3],
            "body_battery_component": row[4],
        }
        for row in recovery_rows
    ]

    sleep_rows = db.fetchall(
        "SELECT date, sleep_score, total_sleep_sec FROM sleep_records WHERE date >= ? ORDER BY date",
        [str(cutoff)],
    )
    sleep_data = [
        {"date": row[0], "sleep_score": row[1], "total_sleep_sec": row[2]}
        for row in sleep_rows
    ]

    hrv_rows = db.fetchall(
        "SELECT date, hrv_last_night_ms, baseline_low_ms, hrv_status "
        "FROM hrv_daily WHERE date >= ? ORDER BY date",
        [str(cutoff)],
    )
    hrv_data = [
        {
            "date": row[0],
            "hrv_last_night_ms": row[1],
            "baseline_low_ms": row[2],
            "hrv_status": row[3],
        }
        for row in hrv_rows
    ]

    anomalies.extend(
        check_recovery_anomalies(recovery_scores, sleep_data, hrv_data)
    )

    # Sort: critical > warning > info.
    severity_order = {
        AnomalySeverity.critical: 0,
        AnomalySeverity.warning: 1,
        AnomalySeverity.info: 2,
    }
    anomalies.sort(key=lambda a: severity_order.get(a.severity, 3))

    return anomalies


# ---------------------------------------------------------------------------
# Overtraining Risk Checks
# ---------------------------------------------------------------------------


def check_overtraining_risk(
    ctl_series: list[dict],
    atl_series: list[dict],
) -> list[Anomaly]:
    """Detect overtraining risk from training load metrics.

    Checks
    ------
    - TSB < -30 for 3+ consecutive days -> warning "Deep fatigue"
    - TSB < -40 -> critical "Severe overreaching"
    - ATL/CTL ratio > 1.5 -> warning "Acute load spike"
    - CTL ramp rate > 7 TSS/week -> warning "Ramp rate too high"
    - Monotony > 2.0 -> warning "Training too monotonous"
    - Weekly strain > 2x previous week -> warning "Strain spike"

    Parameters
    ----------
    ctl_series:
        List of dicts with keys ``date``, ``ctl``, ``atl``, ``tsb``,
        ``monotony``, ``strain``, ``daily_tss``.
    atl_series:
        Same data (alternative view); typically identical to ``ctl_series``.

    Returns
    -------
    list[Anomaly]
    """
    anomalies: list[Anomaly] = []

    if not ctl_series:
        return anomalies

    # ---- TSB checks ----
    consecutive_deep = 0
    for entry in ctl_series:
        tsb = entry.get("tsb")
        if tsb is None:
            consecutive_deep = 0
            continue

        # Critical: TSB < -40
        if tsb < -40:
            anomalies.append(
                _make_anomaly(
                    anomaly_type="overtraining",
                    severity=AnomalySeverity.critical,
                    metric_name="tsb",
                    description=(
                        f"Severe overreaching: TSB = {tsb:.1f} on {entry['date']}. "
                        "Immediate recovery recommended."
                    ),
                    actual_value=tsb,
                    expected_value=-20.0,
                    date_range_start=entry["date"] if isinstance(entry["date"], datetime.date) else None,
                )
            )

        # Warning: TSB < -30 for 3+ consecutive days
        if tsb < -30:
            consecutive_deep += 1
        else:
            consecutive_deep = 0

        if consecutive_deep >= 3:
            anomalies.append(
                _make_anomaly(
                    anomaly_type="overtraining",
                    severity=AnomalySeverity.warning,
                    metric_name="tsb",
                    description=(
                        f"Deep fatigue: TSB below -30 for {consecutive_deep} consecutive days. "
                        "Consider a recovery block."
                    ),
                    actual_value=tsb,
                    expected_value=-20.0,
                )
            )

    # ---- ATL/CTL ratio ----
    for entry in ctl_series:
        ctl = entry.get("ctl", 0)
        atl = entry.get("atl", 0)
        if ctl > 0:
            ratio = atl / ctl
            if ratio > 1.5:
                anomalies.append(
                    _make_anomaly(
                        anomaly_type="overtraining",
                        severity=AnomalySeverity.warning,
                        metric_name="atl_ctl_ratio",
                        description=(
                            f"Acute load spike: ATL/CTL ratio = {ratio:.2f} on {entry['date']}. "
                            "Sudden training load increase risks injury."
                        ),
                        actual_value=ratio,
                        expected_value=1.3,
                    )
                )

    # ---- CTL ramp rate (TSS/week) ----
    if len(ctl_series) >= 14:
        # Compare CTL from 2 weeks ago to current.
        ctl_current = ctl_series[-1].get("ctl", 0)
        ctl_2w_ago = ctl_series[-14].get("ctl", 0)
        ramp_per_week = (ctl_current - ctl_2w_ago) / 2.0
        if ramp_per_week > 7.0:
            anomalies.append(
                _make_anomaly(
                    anomaly_type="overtraining",
                    severity=AnomalySeverity.warning,
                    metric_name="ctl_ramp_rate",
                    description=(
                        f"Ramp rate too high: {ramp_per_week:.1f} TSS/week "
                        f"(safe range: 3-7 TSS/week)."
                    ),
                    actual_value=ramp_per_week,
                    expected_value=5.0,
                )
            )

    # ---- Monotony ----
    for entry in ctl_series[-7:]:  # Check last week.
        monotony = entry.get("monotony")
        if monotony is not None and monotony > 2.0:
            anomalies.append(
                _make_anomaly(
                    anomaly_type="overtraining",
                    severity=AnomalySeverity.warning,
                    metric_name="monotony",
                    description=(
                        f"Training too monotonous: monotony = {monotony:.2f} "
                        f"on {entry['date']} (threshold: 2.0). "
                        "Vary session intensity and duration."
                    ),
                    actual_value=monotony,
                    expected_value=1.5,
                )
            )
            break  # One warning is enough for the week.

    # ---- Strain spike (weekly TSS comparison) ----
    if len(ctl_series) >= 14:
        recent_7 = ctl_series[-7:]
        prev_7 = ctl_series[-14:-7]
        recent_strain = sum(e.get("daily_tss", 0) or 0 for e in recent_7)
        prev_strain = sum(e.get("daily_tss", 0) or 0 for e in prev_7)
        if prev_strain > 0 and recent_strain > 2.0 * prev_strain:
            anomalies.append(
                _make_anomaly(
                    anomaly_type="overtraining",
                    severity=AnomalySeverity.warning,
                    metric_name="weekly_strain",
                    description=(
                        f"Strain spike: this week's TSS ({recent_strain:.0f}) is "
                        f"more than 2x last week ({prev_strain:.0f})."
                    ),
                    actual_value=recent_strain,
                    expected_value=prev_strain * 1.3,
                )
            )

    return anomalies


# ---------------------------------------------------------------------------
# Heart Rate Anomaly Checks
# ---------------------------------------------------------------------------


def check_hr_anomalies(
    recent_activities: list[dict],
    baseline_days: int = 30,
) -> list[Anomaly]:
    """Detect heart rate anomalies from recent activity data.

    Checks
    ------
    - Aerobic decoupling > 10 % in zone-2 sessions -> info
    - Efficiency Factor declining significantly -> info

    Parameters
    ----------
    recent_activities:
        List of dicts with keys ``activity_id``, ``sport_type``, ``date``,
        ``efficiency_factor``, ``aerobic_decoupling_pct``.
    baseline_days:
        Number of days for baseline computation.

    Returns
    -------
    list[Anomaly]
    """
    anomalies: list[Anomaly] = []

    for act in recent_activities:
        decoupling = act.get("aerobic_decoupling_pct")

        if decoupling is not None and decoupling > 10.0:
            anomalies.append(
                _make_anomaly(
                    anomaly_type="hr_anomaly",
                    severity=AnomalySeverity.info,
                    metric_name="aerobic_decoupling",
                    description=(
                        f"Aerobic fitness concern: decoupling = {decoupling:.1f}% "
                        f"in zone-2 session (target: <5%). "
                        f"Activity: {act.get('activity_id', 'unknown')}."
                    ),
                    actual_value=decoupling,
                    expected_value=5.0,
                    activity_id=act.get("activity_id"),
                    sport_type=SportType(act["sport_type"]) if act.get("sport_type") in SportType.__members__ else None,
                )
            )

    return anomalies


# ---------------------------------------------------------------------------
# Performance Decline Checks
# ---------------------------------------------------------------------------


def check_performance_decline(
    metrics: list[dict],
    sport_type: str,
) -> list[Anomaly]:
    """Detect declining performance trends.

    Checks
    ------
    - Efficiency Factor declining trend over 4+ weeks -> warning

    Parameters
    ----------
    metrics:
        List of dicts with keys ``sport_type``, ``date``,
        ``efficiency_factor``.
    sport_type:
        Filter to this sport type.

    Returns
    -------
    list[Anomaly]
    """
    anomalies: list[Anomaly] = []

    # Filter to the target sport.
    sport_metrics = [
        m for m in metrics
        if m.get("sport_type") == sport_type and m.get("efficiency_factor") is not None
    ]

    if len(sport_metrics) < 8:
        # Need at least ~4 weeks of data (2 sessions/week).
        return anomalies

    ef_values = [m["efficiency_factor"] for m in sport_metrics]

    # Check for declining trend using linear regression on the last 28 days
    # worth of EF values.
    recent_ef = ef_values[-min(len(ef_values), 20):]
    if len(recent_ef) < 8:
        return anomalies

    x = np.arange(len(recent_ef), dtype=np.float64)
    y = np.array(recent_ef, dtype=np.float64)

    # Simple linear regression.
    mean_x = np.mean(x)
    mean_y = np.mean(y)
    numerator = np.sum((x - mean_x) * (y - mean_y))
    denominator = np.sum((x - mean_x) ** 2)

    if denominator == 0:
        return anomalies

    slope = float(numerator / denominator)

    # Declining trend: negative slope with meaningful magnitude.
    # Normalise by mean EF to get percentage decline per session.
    if mean_y > 0:
        pct_decline_per_session = (slope / mean_y) * 100.0
    else:
        pct_decline_per_session = 0.0

    if pct_decline_per_session < -0.5:  # > 0.5% decline per session
        anomalies.append(
            _make_anomaly(
                anomaly_type="performance_decline",
                severity=AnomalySeverity.warning,
                metric_name="efficiency_factor",
                description=(
                    f"EF declining trend ({sport_type}): "
                    f"{pct_decline_per_session:.1f}% per session over "
                    f"{len(recent_ef)} sessions. "
                    "May indicate accumulated fatigue or detraining."
                ),
                sport_type=SportType(sport_type) if sport_type in SportType.__members__ else None,
                actual_value=recent_ef[-1],
                expected_value=recent_ef[0],
            )
        )

    return anomalies


# ---------------------------------------------------------------------------
# Recovery Anomaly Checks
# ---------------------------------------------------------------------------


def check_recovery_anomalies(
    recovery_scores: list[dict],
    sleep_data: list[dict],
    hrv_data: list[dict],
) -> list[Anomaly]:
    """Detect anomalies in recovery, sleep, and HRV data.

    Checks
    ------
    - Recovery score < 40 for 3+ days -> warning
    - HRV suppressed below baseline for 3+ days -> warning
    - Sleep score declining trend -> info
    - Body Battery not recovering above 60 overnight -> warning

    Parameters
    ----------
    recovery_scores:
        List of dicts with keys ``date``, ``recovery_score``,
        ``body_battery_component``.
    sleep_data:
        List of dicts with keys ``date``, ``sleep_score``.
    hrv_data:
        List of dicts with keys ``date``, ``hrv_last_night_ms``,
        ``baseline_low_ms``, ``hrv_status``.

    Returns
    -------
    list[Anomaly]
    """
    anomalies: list[Anomaly] = []

    # ---- Recovery score < 40 for 3+ days ----
    if len(recovery_scores) >= 3:
        consecutive_low = 0
        for entry in recovery_scores:
            score = entry.get("recovery_score")
            if score is not None and score < 40:
                consecutive_low += 1
            else:
                consecutive_low = 0

        if consecutive_low >= 3:
            anomalies.append(
                _make_anomaly(
                    anomaly_type="recovery",
                    severity=AnomalySeverity.warning,
                    metric_name="recovery_score",
                    description=(
                        f"Recovery score below 40 for {consecutive_low} consecutive days. "
                        "Extended recovery or reduced training volume recommended."
                    ),
                    actual_value=recovery_scores[-1].get("recovery_score"),
                    expected_value=60.0,
                )
            )

    # ---- HRV suppression ----
    if len(hrv_data) >= 3:
        consecutive_suppressed = 0
        for entry in hrv_data:
            hrv_val = entry.get("hrv_last_night_ms")
            baseline_low = entry.get("baseline_low_ms")
            if (
                hrv_val is not None
                and baseline_low is not None
                and hrv_val < baseline_low
            ):
                consecutive_suppressed += 1
            else:
                consecutive_suppressed = 0

        if consecutive_suppressed >= 3:
            anomalies.append(
                _make_anomaly(
                    anomaly_type="recovery",
                    severity=AnomalySeverity.warning,
                    metric_name="hrv_suppression",
                    description=(
                        f"HRV suppressed below baseline for {consecutive_suppressed} "
                        "consecutive days. Sign of accumulated fatigue or illness."
                    ),
                    actual_value=hrv_data[-1].get("hrv_last_night_ms"),
                    expected_value=hrv_data[-1].get("baseline_low_ms"),
                )
            )

    # ---- Sleep score declining trend ----
    if len(sleep_data) >= 14:
        recent_sleep = [
            s["sleep_score"]
            for s in sleep_data[-14:]
            if s.get("sleep_score") is not None
        ]
        if len(recent_sleep) >= 10:
            x = np.arange(len(recent_sleep), dtype=np.float64)
            y = np.array(recent_sleep, dtype=np.float64)
            mean_x = np.mean(x)
            mean_y = np.mean(y)
            num = np.sum((x - mean_x) * (y - mean_y))
            den = np.sum((x - mean_x) ** 2)
            if den > 0:
                slope = float(num / den)
                # Significant decline: > 1 point per day.
                if slope < -1.0:
                    anomalies.append(
                        _make_anomaly(
                            anomaly_type="recovery",
                            severity=AnomalySeverity.info,
                            metric_name="sleep_score_trend",
                            description=(
                                f"Sleep score declining: {slope:.1f} points/day "
                                "over the last 2 weeks. Review sleep habits."
                            ),
                            actual_value=recent_sleep[-1],
                            expected_value=recent_sleep[0],
                        )
                    )

    # ---- Body Battery not recovering above 60 ----
    if len(recovery_scores) >= 3:
        consecutive_low_bb = 0
        for entry in recovery_scores:
            bb = entry.get("body_battery_component")
            if bb is not None and bb < 60:
                consecutive_low_bb += 1
            else:
                consecutive_low_bb = 0

        if consecutive_low_bb >= 3:
            anomalies.append(
                _make_anomaly(
                    anomaly_type="recovery",
                    severity=AnomalySeverity.warning,
                    metric_name="body_battery",
                    description=(
                        f"Body Battery not recovering above 60 for "
                        f"{consecutive_low_bb} consecutive days. "
                        "May indicate chronic stress or sleep deficit."
                    ),
                    actual_value=recovery_scores[-1].get("body_battery_component"),
                    expected_value=60.0,
                )
            )

    return anomalies
