"""Unit tests for the AI Time Based thermal-learning logic."""

from datetime import datetime, timedelta

import pytest

from custom_components.dual_smart_thermostat.control.thermal_learning import (
    DEFAULT_HEATING_POWER,
    MAX_HEATING_POWER,
    MIN_HEATING_POWER,
    HeatingPowerTracker,
    HeatLossTracker,
    compute_env_factor,
    compute_weight_factor,
    heating_power_valve_fraction,
)

T0 = datetime(2024, 1, 1, 12, 0, 0)


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


class TestValveControlLaw:
    def test_zero_or_negative_error_is_closed(self):
        assert heating_power_valve_fraction(0.0, 0.01) == 0.0
        assert heating_power_valve_fraction(-1.0, 0.01) == 0.0

    def test_slow_room_opens_more_than_fast_room(self):
        # Same error, lower heating_power (slower room) -> larger valve opening.
        slow = heating_power_valve_fraction(0.5, 0.005)
        fast = heating_power_valve_fraction(0.5, 0.02)
        assert slow > fast

    def test_large_error_enforces_minimum_opening(self):
        # Even with a very fast room, a large error keeps a minimum opening.
        valve = heating_power_valve_fraction(1.0, MAX_HEATING_POWER)
        assert valve >= 0.15

    def test_result_bounded_0_1(self):
        assert heating_power_valve_fraction(10.0, MIN_HEATING_POWER) == 1.0

    def test_matches_reference_value(self):
        # BT reference: hp=0.01, error=0.2 -> ~0.323.
        assert heating_power_valve_fraction(0.2, 0.01) == pytest.approx(0.323, abs=0.01)


class TestFactors:
    def test_env_factor_without_data_is_one(self):
        assert compute_env_factor(None, 20.0) == 1.0

    def test_env_factor_bounds(self):
        assert compute_env_factor(-50.0, 20.0) == 1.3
        assert compute_env_factor(19.0, 20.0) == 0.7

    def test_weight_factor_bounds(self):
        assert compute_weight_factor(None, 18.0, 22.0) == 1.0
        assert 0.5 <= compute_weight_factor(18.0, 18.0, 22.0) <= 1.5


class TestHeatingPowerTracker:
    def test_learns_from_a_heating_cycle(self):
        tr = HeatingPowerTracker(heating_power=DEFAULT_HEATING_POWER)

        # Heating starts at 18.0.
        tr.update(18.0, True, _at(0), target_temp=21.0)
        # ... rises while heating.
        tr.update(18.5, True, _at(10), target_temp=21.0)
        # Heating stops at 19.0 (peak candidate).
        tr.update(19.0, False, _at(20), target_temp=21.0)
        # Temperature now falls below the peak -> finalize.
        changed = tr.update(18.9, False, _at(25), target_temp=21.0)

        assert changed is True
        # rate over the cycle = (19.0 - 18.0) / 20 min = 0.05 °C/min; EMA pulls
        # the default 0.01 upward.
        assert tr.heating_power > DEFAULT_HEATING_POWER
        assert MIN_HEATING_POWER <= tr.heating_power <= MAX_HEATING_POWER

    def test_no_change_without_completed_cycle(self):
        tr = HeatingPowerTracker()
        assert tr.update(18.0, True, _at(0)) is False
        assert tr.update(18.4, True, _at(5)) is False
        assert tr.heating_power == DEFAULT_HEATING_POWER

    def test_tracks_target_range(self):
        tr = HeatingPowerTracker()
        tr.update(18.0, True, _at(0), target_temp=23.0)
        assert tr.max_target >= 23.0


class TestHeatLossTracker:
    def test_learns_from_idle_cooling(self):
        tr = HeatLossTracker()
        # Idle cooling from 21 -> 20 over 50 min.
        tr.update(21.0, False, _at(0))
        tr.update(20.5, False, _at(25))
        tr.update(20.0, False, _at(50))
        # Heating restarts -> finalize the cooling cycle.
        changed = tr.update(20.0, True, _at(50))

        assert changed is True
        # drop 1.0 °C over 50 min = 0.02 °C/min; EMA pulls 0.01 up.
        assert tr.heat_loss_rate > 0.01

    def test_window_open_resets(self):
        tr = HeatLossTracker()
        tr.update(21.0, False, _at(0))
        tr.update(20.5, False, _at(20), window_open=True)
        # Window reset cleared tracking; restart heating shouldn't finalize.
        assert tr.update(20.5, True, _at(21)) is False
        assert tr.heat_loss_rate == 0.01
