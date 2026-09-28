"""hart analytics: pure computation over the training data."""

from hart.analytics.pace import (
    grade_adjusted_pace,
    normalized_graded_pace,
    pace_to_speed,
    run_tss,
    speed_to_pace,
)
from hart.analytics.power import (
    apply_power_balance,
    normalized_power,
    power_duration_curve,
    tss_from_power,
    variability_index,
)
from hart.analytics.swim import (
    pace_per_100m,
    swim_css,
    swim_tss,
    swolf,
)
from hart.analytics.training_load import (
    compute_ctl_atl_tsb,
    compute_monotony_strain,
    hr_trimp,
    update_training_load,
)
from hart.analytics.anomaly import (
    check_hr_anomalies,
    check_overtraining_risk,
    check_performance_decline,
    check_recovery_anomalies,
    detect_anomalies,
    z_score,
)
from hart.analytics.compliance import (
    volume_trend_compliance,
    weekly_compliance,
)
from hart.analytics.efficiency import (
    aerobic_decoupling,
    cardiac_drift_rate,
    compute_decoupling_from_streams,
    steady_session_decoupling,
    efficiency_factor_bike,
    efficiency_factor_run,
)
from hart.analytics.hrv import (
    detect_hrv_suppression,
    hrv_coefficient_of_variation,
    hrv_trend_analysis,
    ln_rmssd,
    rmssd,
)
from hart.analytics.recovery import (
    compute_recovery_score,
    recovery_trend,
)

__all__ = [
    # power
    "apply_power_balance",
    "normalized_power",
    "power_duration_curve",
    "tss_from_power",
    "variability_index",
    # pace
    "grade_adjusted_pace",
    "normalized_graded_pace",
    "pace_to_speed",
    "run_tss",
    "speed_to_pace",
    # swim
    "pace_per_100m",
    "swim_css",
    "swim_tss",
    "swolf",
    # training_load
    "compute_ctl_atl_tsb",
    "compute_monotony_strain",
    "hr_trimp",
    "update_training_load",
    # efficiency
    "aerobic_decoupling",
    "cardiac_drift_rate",
    "compute_decoupling_from_streams",
    "steady_session_decoupling",
    "efficiency_factor_bike",
    "efficiency_factor_run",
    # hrv
    "detect_hrv_suppression",
    "hrv_coefficient_of_variation",
    "hrv_trend_analysis",
    "ln_rmssd",
    "rmssd",
    # recovery
    "compute_recovery_score",
    "recovery_trend",
    # anomaly
    "check_hr_anomalies",
    "check_overtraining_risk",
    "check_performance_decline",
    "check_recovery_anomalies",
    "detect_anomalies",
    "z_score",
    # compliance
    "volume_trend_compliance",
    "weekly_compliance",
]
