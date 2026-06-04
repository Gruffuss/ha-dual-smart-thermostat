"""Unit tests for the pure heating-control algorithms (TPI duty + PWM planning)."""

import pytest

from custom_components.dual_smart_thermostat.control.pwm import PwmPlan, plan_pwm
from custom_components.dual_smart_thermostat.control.tpi import (
    TpiParams,
    compute_tpi_duty,
)


class TestComputeTpiDuty:
    """Tests for the TPI duty-cycle formula."""

    def test_below_target_produces_positive_duty(self):
        # error = 2.0, coef_int 0.6 -> 1.2 -> 120% clamped to 100
        duty = compute_tpi_duty(18.0, 20.0, None, TpiParams())
        assert duty == 100.0

    def test_small_error_scales_proportionally(self):
        # error = 0.5, coef_int 0.6 -> 0.3 -> 30%
        duty = compute_tpi_duty(19.5, 20.0, None, TpiParams())
        assert duty == pytest.approx(30.0)

    def test_at_target_is_zero(self):
        assert compute_tpi_duty(20.0, 20.0, None, TpiParams()) == 0.0

    def test_overshoot_beyond_threshold_forces_zero(self):
        # 0.5 °C above target, threshold 0.3 -> off
        assert compute_tpi_duty(20.5, 20.0, None, TpiParams()) == 0.0

    def test_slightly_above_target_within_threshold_is_zero_but_not_cut(self):
        # 0.2 °C above target: not past overshoot cutoff, but negative error
        # yields a negative duty that clamps to 0.
        assert compute_tpi_duty(20.2, 20.0, None, TpiParams()) == 0.0

    def test_outdoor_term_adds_compensation(self):
        # error 0.5 -> 0.3; outdoor term coef_ext 0.01 * (20 - 0) = 0.2 -> total
        # 0.5 -> 50%
        duty = compute_tpi_duty(19.5, 20.0, 0.0, TpiParams())
        assert duty == pytest.approx(50.0)

    def test_missing_temps_returns_zero(self):
        assert compute_tpi_duty(None, 20.0, None, TpiParams()) == 0.0
        assert compute_tpi_duty(19.0, None, None, TpiParams()) == 0.0

    def test_custom_coefficients(self):
        params = TpiParams(coef_int=1.0, coef_ext=0.0)
        # error 0.4 * 1.0 -> 40%
        assert compute_tpi_duty(19.6, 20.0, 5.0, params) == pytest.approx(40.0)

    def test_duty_is_clamped_to_100(self):
        assert compute_tpi_duty(10.0, 20.0, -10.0, TpiParams()) == 100.0


class TestPlanPwm:
    """Tests for splitting a duty into on/off segments."""

    def test_half_duty_splits_evenly(self):
        plan = plan_pwm(50.0, 1800.0)
        assert plan == PwmPlan(on_seconds=900.0, off_seconds=900.0)

    def test_zero_duty_is_always_off(self):
        plan = plan_pwm(0.0, 1800.0)
        assert plan.always_off
        assert plan.on_seconds == 0.0

    def test_full_duty_is_always_on(self):
        plan = plan_pwm(100.0, 1800.0)
        assert plan.always_on
        assert plan.off_seconds == 0.0

    def test_duty_clamped_above_100(self):
        plan = plan_pwm(150.0, 1800.0)
        assert plan.always_on

    def test_short_on_segment_collapses_to_off(self):
        # 5% of 1800s = 90s on, below the 300s minimum -> stay off.
        plan = plan_pwm(5.0, 1800.0, min_segment_seconds=300.0)
        assert plan == PwmPlan(on_seconds=0.0, off_seconds=1800.0)

    def test_short_off_segment_collapses_to_on(self):
        # 95% of 1800s = 90s off, below the 300s minimum -> stay on.
        plan = plan_pwm(95.0, 1800.0, min_segment_seconds=300.0)
        assert plan == PwmPlan(on_seconds=1800.0, off_seconds=0.0)

    def test_segment_above_minimum_is_kept(self):
        # 20% of 1800s = 360s on > 300s minimum -> kept.
        plan = plan_pwm(20.0, 1800.0, min_segment_seconds=300.0)
        assert plan.on_seconds == pytest.approx(360.0)
        assert plan.off_seconds == pytest.approx(1440.0)

    def test_no_minimum_keeps_exact_split(self):
        plan = plan_pwm(10.0, 1800.0)
        assert plan.on_seconds == pytest.approx(180.0)
        assert plan.off_seconds == pytest.approx(1620.0)
