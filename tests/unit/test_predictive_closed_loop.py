"""V5 plants driven minute by minute through the pure controller and PWM."""

from __future__ import annotations

from functools import lru_cache
import math
import random

import pytest

from custom_components.dual_smart_thermostat.control.predictive import (
    MinuteInputs,
    PredictiveController,
    PredictiveSettings,
)
from tests.unit.test_predictive_controller import Driver
from tests.unit.test_predictive_model import plant_step


@lru_cache(maxsize=None)
def simulate(mode, solar=False, gated=False, min_off_minutes=None):
    theta = (0.03, 0.0015, 0)
    solar_outdoor_mean, solar_outdoor_amplitude = 8, 5
    if solar:
        min_outdoor = solar_outdoor_mean - solar_outdoor_amplitude
        assert theta[0] / theta[1] + min_outdoor >= 21 + 1
    ctl = PredictiveController(
        PredictiveSettings(30, 10, gate=mode, min_off_minutes=min_off_minutes)
    )
    driver = Driver(ctl)
    temp, slab = (21, 0.8) if solar else (18, 0)
    on_minutes = hot_run = max_hot_run = 0
    rng = random.Random(42)
    promotions, demotions, transitions = [], [], []
    temperatures = []
    demand = True
    for minute in range((10 if solar else 3) * 1440):
        outdoor = (
            (
                solar_outdoor_mean
                + solar_outdoor_amplitude * math.sin(2 * math.pi * minute / 1440)
            )
            if solar
            else 5
        )
        reading = round(temp + (rng.gauss(0, 0.05) if solar else 0), 1)
        if solar and gated:
            if reading < 20.7:
                demand = True
            elif reading >= 21:
                demand = False
        before = ctl.mode
        valve = driver.tick(
            MinuteInputs(minute, reading, outdoor, driver.valve, 21, demand, True)
        )
        after = ctl.mode
        if before != after:
            (promotions if after == "predictive" else demotions).append(minute)
            transitions.append(
                (minute, after, ctl.tracker.mae120, ctl.tracker.persistence_mae120)
            )
        on_minutes += valve
        hot_run = hot_run + 1 if valve and temp > 21.3 else 0
        max_hot_run = max(max_hot_run, hot_run)
        temp, slab = plant_step(temp, slab, outdoor, valve, theta=theta)
        time_of_day = minute % 1440
        if solar and 600 <= time_of_day <= 960:
            temp += 0.01 * math.sin(math.pi * (time_of_day - 600) / 360)
        temperatures.append(temp)
    result = {
        "overshoot": max(0, max(temperatures) - 21),
        "mae_last24h": sum(abs(t - 21) for t in temperatures[-1440:]) / 1440,
        "valve_on_minutes": on_minutes,
        "max_hot_run": max_hot_run,
        "minutes_below_sp_minus_0_7": sum(t < 20.3 for t in temperatures),
        "minutes_below_sp_minus_0_3": sum(t < 20.7 for t in temperatures),
        "first_cold_minute": next(
            (i for i, t in enumerate(temperatures) if t < 20.3), None
        ),
        "min_temp": min(temperatures),
        "promotions_minute": promotions,
        "demotions_minute": demotions,
        "transitions": transitions,
    }
    print(
        f"\nCLOSED_LOOP mode={mode} solar={solar} gated={gated} "
        f"min_off={ctl.settings.min_off_minutes} {result}"
    )
    return result


def test_predictive_closed_loop_limits():
    predictive = simulate("on")
    assert predictive["overshoot"] <= 0.3
    assert predictive["mae_last24h"] <= 0.2
    assert predictive["max_hot_run"] < 240


def test_predictive_tracks_better_than_pi():
    pi, predictive = simulate("off"), simulate("on")
    assert predictive["mae_last24h"] < pi["mae_last24h"]
    assert predictive["overshoot"] <= 0.3


def assert_solar_margin(pi, predictive):
    assert (
        predictive["minutes_below_sp_minus_0_3"]
        <= pi["minutes_below_sp_minus_0_3"] + 60
    )
    assert (
        predictive["minutes_below_sp_minus_0_7"]
        <= pi["minutes_below_sp_minus_0_7"] + 60
    )
    assert predictive["min_temp"] >= pi["min_temp"] - 0.1
    assert predictive["overshoot"] <= 0.3 + pi["overshoot"]


def test_predictive_auto_not_colder_than_pi_with_solar_mismatch_gated():
    pi = simulate("off", solar=True, gated=True, min_off_minutes=0)
    predictive = simulate("auto", solar=True, gated=True, min_off_minutes=0)
    assert_solar_margin(pi, predictive)
    assert_v5_measurements(pi, 3288, 7866, 19.649872839, 0.281292166)
    assert_v5_measurements(predictive, 3175, 7752, 19.673562927, 0.281292166)
    assert pi["promotions_minute"] == pi["demotions_minute"] == []
    assert predictive["promotions_minute"] == [10080]
    assert predictive["demotions_minute"] == [10905]


def assert_v5_measurements(result, below_07, below_03, min_temp, overshoot):
    """Published final-v5 results, with an absolute 1e-6 temperature tolerance."""
    assert result["minutes_below_sp_minus_0_7"] == below_07
    assert result["minutes_below_sp_minus_0_3"] == below_03
    assert result["min_temp"] == pytest.approx(min_temp, abs=1e-6, rel=0)
    assert result["overshoot"] == pytest.approx(overshoot, abs=1e-6, rel=0)


def test_predictive_port_matches_v5_always_on_diagnostic():
    pi = simulate("off", solar=True, min_off_minutes=0)
    predictive = simulate("auto", solar=True, min_off_minutes=0)
    assert_v5_measurements(pi, 2124, 3999, 19.726638896, 0.486864199)
    assert_v5_measurements(predictive, 2228, 4080, 19.726638896, 0.503619152)
    assert pi["promotions_minute"] == pi["demotions_minute"] == []
    assert predictive["promotions_minute"] == [10080, 14220]
    assert predictive["demotions_minute"] == [10665, 14222]


def test_predictive_auto_not_colder_than_pi_with_solar_mismatch():
    assert_solar_margin(simulate("off", solar=True), simulate("auto", solar=True))
