"""Tests for training load analytics."""

import pytest


class TestTSS:
    """Test Training Stress Score calculations."""

    def test_bike_tss_from_power(self):
        from hart.analytics.power import normalized_power, tss_from_power

        # 1 hour at FTP should give TSS ≈ 100
        ftp = 250
        power_series = [250.0] * 3600  # constant FTP for 1 hour
        np = normalized_power(power_series)
        tss = tss_from_power(3600, np, ftp)
        assert 95 < tss < 105, f"1hr at FTP should be ~100 TSS, got {tss}"

    def test_bike_tss_half_ftp(self):
        from hart.analytics.power import normalized_power, tss_from_power

        ftp = 250
        power_series = [125.0] * 3600  # half FTP for 1 hour
        np = normalized_power(power_series)
        tss = tss_from_power(3600, np, ftp)
        assert 20 < tss < 30, f"1hr at 50% FTP should be ~25 TSS, got {tss}"

    def test_run_tss(self):
        from hart.analytics.pace import run_tss

        # Running at threshold pace for 1 hour should give rTSS ≈ 100
        ftp_pace = 300  # 5:00/km
        rtss = run_tss(3600, 300, ftp_pace)
        assert 95 < rtss < 105, f"1hr at FTP pace should be ~100 rTSS, got {rtss}"

    def test_swim_tss(self):
        from hart.analytics.swim import swim_tss

        # Swimming at CSS for 1 hour should give sTSS ≈ 100
        css = 105  # 1:45/100m
        stss = swim_tss(3600, 105, css)
        assert 95 < stss < 105, f"1hr at CSS should be ~100 sTSS, got {stss}"

    def test_hr_trimp(self):
        from hart.analytics.training_load import hr_trimp

        # 60 min at 70% HRR should give a moderate TRIMP
        trimp = hr_trimp(3600, 150, 50, 195, "male")
        assert trimp > 0, "TRIMP should be positive"
        assert trimp < 300, f"TRIMP seems too high: {trimp}"


class TestCTLATLTSB:
    """Test fitness/fatigue model."""

    def test_ctl_atl_tsb_basic(self, sample_daily_tss):
        from hart.analytics.training_load import compute_ctl_atl_tsb

        result = compute_ctl_atl_tsb(sample_daily_tss)
        assert len(result) == len(sample_daily_tss)

        # CTL should be smoothly increasing with training
        last = result[-1]
        assert last["ctl"] > 0, "CTL should be positive after training"
        assert last["atl"] > 0, "ATL should be positive after training"
        assert "tsb" in last, "TSB should be computed"
        assert last["tsb"] == pytest.approx(last["ctl"] - last["atl"], abs=0.01)

    def test_ctl_responds_slowly(self, sample_daily_tss):
        from hart.analytics.training_load import compute_ctl_atl_tsb

        result = compute_ctl_atl_tsb(sample_daily_tss)

        # ATL should respond faster than CTL (lower time constant)
        # After a high TSS day, ATL should be higher than CTL
        for i, (d, tss) in enumerate(sample_daily_tss):
            if tss > 100 and i > 0:
                # The day after a big day, ATL should spike more than CTL
                assert result[i]["atl"] > result[i - 1]["atl"], "ATL should spike after high TSS"
                break

    def test_rest_day_tsb_improves(self, sample_daily_tss):
        from hart.analytics.training_load import compute_ctl_atl_tsb

        result = compute_ctl_atl_tsb(sample_daily_tss)

        # After a rest day (TSS=0), TSB should improve (become more positive)
        for i, (d, tss) in enumerate(sample_daily_tss):
            if tss == 0 and i > 5:
                if result[i - 1]["atl"] > result[i - 1]["ctl"]:
                    assert result[i]["tsb"] > result[i - 1]["tsb"], "TSB should improve on rest day when fatigued"
                break

    def test_monotony_strain(self):
        from hart.analytics.training_load import compute_monotony_strain

        # Varied training with some consistency = moderate monotony
        moderate_tss = [55, 60, 58, 62, 57, 60, 59]
        monotony_m, strain_m = compute_monotony_strain(moderate_tss)
        assert monotony_m > 1, f"Similar daily TSS should have monotony > 1, got {monotony_m}"

        # Highly varied training = lower monotony
        varied_tss = [0, 80, 40, 100, 50, 70, 0]
        monotony_v, strain_v = compute_monotony_strain(varied_tss)
        assert monotony_v < monotony_m, "More varied training should have lower monotony"

        # Identical daily TSS = stdev 0, monotony returns 0 (div by zero guard)
        same_tss = [60.0] * 7
        monotony_s, _ = compute_monotony_strain(same_tss)
        assert monotony_s == 0, "Identical values have stdev=0, monotony should be 0"


class TestNormalizedPower:
    """Test bike power calculations."""

    def test_constant_power(self):
        from hart.analytics.power import normalized_power

        # Constant power: NP should equal avg power
        power_series = [200.0] * 3600
        np = normalized_power(power_series)
        assert abs(np - 200) < 2, f"NP of constant power should ≈ avg power, got {np}"

    def test_variable_power_higher_np(self):
        from hart.analytics.power import normalized_power

        # Variable power in longer blocks should have NP > avg power
        # (alternating every second averages out in the 30s window, so use blocks)
        power_series = ([150.0] * 60 + [250.0] * 60) * 30  # 60s blocks
        np_val = normalized_power(power_series)
        avg = sum(power_series) / len(power_series)
        assert np_val > avg, f"NP ({np_val}) should be > avg ({avg}) for variable power blocks"

    def test_power_duration_curve(self, sample_power_series):
        from hart.analytics.power import power_duration_curve

        pdc = power_duration_curve(sample_power_series)
        # Shorter durations should have higher best power
        assert pdc[1] >= pdc[60], "1s power should be >= 1min power"
        assert pdc[60] >= pdc[3600] or 3600 not in pdc, "1min power should be >= 1hr power"
