"""Tests for recovery and HRV analytics."""


class TestHRV:
    """Test HRV analysis functions."""

    def test_rmssd_basic(self):
        from hart.analytics.hrv import rmssd

        # Known RR intervals
        rr = [800, 810, 795, 805, 800]
        result = rmssd(rr)
        assert result > 0, "RMSSD should be positive"

        # Higher variability = higher RMSSD
        rr_low_var = [800, 801, 800, 801, 800]
        rr_high_var = [780, 820, 770, 830, 790]
        assert rmssd(rr_high_var) > rmssd(rr_low_var), "Higher HRV variability should give higher RMSSD"

    def test_ln_rmssd(self):
        from hart.analytics.hrv import ln_rmssd

        rr = [800, 810, 795, 805, 800]
        result = ln_rmssd(rr)
        assert result > 0, "ln(RMSSD) should be positive for normal HR"

    def test_hrv_trend_analysis(self):
        from datetime import date, timedelta

        from hart.analytics.hrv import hrv_trend_analysis

        dates = [date(2026, 3, 1) + timedelta(days=i) for i in range(30)]
        # Declining HRV trend
        hrv_values = [50.0 - i * 0.5 for i in range(30)]

        result = hrv_trend_analysis(dates, hrv_values)
        assert result["trend_direction"] == "declining", (
            f"Should detect declining trend, got {result['trend_direction']}"
        )

    def test_hrv_suppression_detection(self):
        from hart.analytics.hrv import detect_hrv_suppression

        # HRV below baseline for 5 days
        hrv_values = [30, 28, 25, 27, 29]
        assert detect_hrv_suppression(hrv_values, baseline_low=35, consecutive_days=3) is True

        # HRV above baseline
        hrv_values = [40, 42, 38, 41, 39]
        assert detect_hrv_suppression(hrv_values, baseline_low=35, consecutive_days=3) is False


class TestRecovery:
    """Test composite recovery score."""

    def test_recovery_score_range(self):
        from hart.analytics.recovery import compute_recovery_score

        health = {"body_battery_start": 80, "avg_stress": 25, "training_readiness": 75}
        sleep = {"total_sleep_sec": 28800, "sleep_score": 85}  # 8 hours
        hrv = {"hrv_last_night_ms": 55, "baseline_low_ms": 40, "baseline_high_ms": 60}
        training_load = {"tsb": 5}
        weights = {
            "hrv": 0.30,
            "sleep": 0.25,
            "body_battery": 0.20,
            "readiness": 0.15,
            "stress": 0.05,
            "fatigue": 0.05,
        }

        score = compute_recovery_score(health, sleep, hrv, training_load, weights)
        assert 0 <= score.recovery_score <= 100, f"Score should be 0-100, got {score.recovery_score}"
        assert score.recovery_score > 60, "Good data should give decent recovery score"

    def test_recovery_score_bad_sleep(self):
        from hart.analytics.recovery import compute_recovery_score

        health = {"body_battery_start": 40, "avg_stress": 60, "training_readiness": 35}
        sleep = {"total_sleep_sec": 18000, "sleep_score": 40}  # 5 hours
        hrv = {"hrv_last_night_ms": 30, "baseline_low_ms": 40, "baseline_high_ms": 60}
        training_load = {"tsb": -25}
        weights = {
            "hrv": 0.30,
            "sleep": 0.25,
            "body_battery": 0.20,
            "readiness": 0.15,
            "stress": 0.05,
            "fatigue": 0.05,
        }

        score = compute_recovery_score(health, sleep, hrv, training_load, weights)
        assert score.recovery_score < 50, "Poor data should give low recovery score"


class TestAnomaly:
    """Test anomaly detection logic."""

    def test_overtraining_risk_deep_fatigue(self):
        from hart.analytics.anomaly import check_overtraining_risk

        # TSB < -30 for 5 consecutive days
        ctl_series = [{"date": f"2026-03-{i:02d}", "ctl": 80, "atl": 115, "tsb": -35} for i in range(1, 6)]
        atl_series = ctl_series  # same data

        anomalies = check_overtraining_risk(ctl_series, atl_series)
        assert len(anomalies) > 0, "Should detect overtraining risk with TSB < -30"
        types = [a.anomaly_type for a in anomalies]
        assert any(
            "overtraining" in t.lower() or "fatigue" in t.lower() or "overreaching" in t.lower() for t in types
        ), f"Should detect overtraining risk, got types: {types}"

    def test_no_anomaly_normal_load(self):
        from hart.analytics.anomaly import check_overtraining_risk

        # Normal TSB around -5 to +5
        ctl_series = [{"date": f"2026-03-{i:02d}", "ctl": 60, "atl": 63, "tsb": -3} for i in range(1, 8)]
        atl_series = ctl_series

        anomalies = check_overtraining_risk(ctl_series, atl_series)
        critical = [a for a in anomalies if a.severity == "critical"]
        assert len(critical) == 0, "Normal load should not have critical anomalies"
