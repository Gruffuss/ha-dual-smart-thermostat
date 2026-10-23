"""One-to-one v5 model and planner regressions, independent of Home Assistant."""

from __future__ import annotations

import itertools
import json
import math
from unittest.mock import patch

import pytest

from custom_components.dual_smart_thermostat.control import predictive_model as heating
from custom_components.dual_smart_thermostat.control.predictive_model import (
    AccuracyTracker,
    RoomModel,
    SlabRLS,
    forecast,
    hold_duty,
    plan_cycle,
)


def plant_step(temp, slab, outdoor, valve, theta=(0.03, 0.0015, 0), tau=120):
    slab += (int(valve) - slab) / tau
    alpha, beta, gamma = theta
    return (temp + alpha * slab - beta * (temp - outdoor) + gamma, slab)


def test_slab_first_order():
    model = SlabRLS(120)
    for _ in range(120):
        model.step_slab(True, 1)
    assert model.slab == pytest.approx(1 - (1 - 1 / 120) ** 120, abs=5e-08, rel=0)


def test_rls_recovers_known_params():
    model = SlabRLS(120)
    temp, slab = (18, 0)
    model.add_minute(temp, 5, True)
    for minute in range(21 * 1440):
        outdoor = 5 + 5 * math.sin(2 * math.pi * minute / 1440)
        valve = minute // 360 % 2 == 0
        temp, slab = plant_step(temp, slab, outdoor, valve)
        model.step_slab(valve, 1)
        model.add_minute(temp, outdoor, True)
    alpha, beta, gamma = model.theta
    assert alpha == pytest.approx(0.03, abs=0.003, rel=0)
    assert beta == pytest.approx(0.0015, abs=0.00015, rel=0)
    assert abs(gamma) < 0.002


def test_rls_invalid_minute_discards_block():
    model = SlabRLS(120)
    model.add_minute(20, 5, True)
    for _ in range(14):
        assert not model.add_minute(20, 5, True)
    assert not model.add_minute(20, 5, False)
    model.add_minute(20, 5, True)
    for _ in range(14):
        assert not model.add_minute(20, 5, True)
    assert model.blocks == 0
    assert model.add_minute(20, 5, True)
    assert model.blocks == 1


def test_room_model_picks_true_tau():
    model = RoomModel()
    temp, slab = (18, 0)
    model.minute(False, temp, 5, True, dt=0)
    for minute in range(21 * 1440):
        outdoor = 5 + 5 * math.sin(2 * math.pi * minute / 1440)
        valve = minute // 360 % 2 == 0
        temp, slab = plant_step(temp, slab, outdoor, valve, tau=150)
        model.minute(valve, temp, outdoor, True)
    assert model.active().tau == 150, {
        tau: (m.theta, m.err) for tau, m in model.models.items()
    }


def test_forecast_matches_closed_form_when_valve_off():
    theta = (0.03, 0.0015, 0.001)
    temp, slab, outdoor, tau = (22, 0.8, 5, 120)
    result = forecast(temp, slab, outdoor, [0] * 240, theta, tau)
    r, q = (1 - theta[1], 1 - 1 / tau)
    equilibrium = outdoor + theta[2] / theta[1]
    for n in (1, 30, 60, 120, 240):
        expected = (
            equilibrium
            + (temp - equilibrium) * r**n
            + theta[0] * slab * q * (r**n - q**n) / (r - q)
        )
        assert result[n - 1] == pytest.approx(expected, abs=5e-11, rel=0)


def test_hold_duty():
    assert hold_duty((0.03, 0.0015, 0), 21, 5) == pytest.approx(0.8, abs=5e-08, rel=0)
    assert hold_duty((0, 0.0015, 0), 21, 5) == 0
    assert hold_duty((0.03, 0.0015, 0), 21, -50) == 1


def test_planner_heats_cold_room():
    on, values = plan_cycle(18, 0, 5, 21, (0.03, 0.0015, 0), 120)
    assert on == 30
    assert len(values) == 240


def test_planner_stops_early_only_when_forecast_reaches_target():
    theta = (0.03, 0.0015, 0)
    charged, values = plan_cycle(20.6, 0.95, 5, 21, theta, 120)
    cold, _ = plan_cycle(20.6, 0, 5, 21, theta, 120)
    assert charged < cold
    if charged == 0:
        assert max(values) >= 21
    assert plan_cycle(21.2, 0.9, 5, 21, theta, 120)[0] == 0


def test_planner_never_below_min_on():
    for temp in (18, 20.5, 20.9, 21, 21.2, 22):
        on, _ = plan_cycle(temp, 0.6, 5, 21, (0.03, 0.0015, 0), 120)
        assert on == 0 or 10 <= on <= 30


def test_accuracy_tracker_perfect_model():
    tracker = AccuracyTracker()
    temp, slab, theta = (18, 0, (0.03, 0.0015, 0))
    tracker.record(0, temp, False, 5, True)
    tracker.snapshot(0, temp, slab, 5, theta, 120)
    for minute in range(1, 841):
        valve = minute // 90 % 2 == 0
        temp, slab = plant_step(temp, slab, 5, valve)
        tracker.record(minute, temp, valve, 5, True)
        if minute % 15 == 0:
            tracker.snapshot(minute, temp, slab, 5, theta, 120)
    assert tracker.n120 >= 48
    assert tracker.mae60 < 0.01
    assert tracker.mae120 < 0.01


def test_accuracy_tracker_skips_invalid_windows():
    tracker = AccuracyTracker()
    tracker.snapshot(0, 20, 0, 5, (0.03, 0.0015, 0), 120)
    for minute in range(1, 121):
        tracker.record(minute, 20, False, 5, minute != 30)
    assert (tracker.n60, tracker.n120) == (0, 0)


def test_room_model_round_trip():
    model = RoomModel()
    for minute in range(50):
        model.minute(minute % 20 < 10, 20 + minute / 100, 5, True)
    restored = RoomModel.from_dict(json.loads(json.dumps(model.to_dict())))
    assert model.to_dict() == restored.to_dict()
    a, b = (model.active(), restored.active())
    assert forecast(20, a.slab, 5, [1] * 120, a.theta, a.tau) == forecast(
        20, b.slab, 5, [1] * 120, b.theta, b.tau
    )


def test_accuracy_round_trip_keeps_scores_not_pending_windows():
    tracker = AccuracyTracker()
    tracker.mae60, tracker.mae120, tracker.n60, tracker.n120 = (0.1, 0.2, 60, 50)
    tracker.snapshot(0, 20, 0, 5, (0.03, 0.0015, 0), 120)
    restored = AccuracyTracker.from_dict(json.loads(json.dumps(tracker.to_dict())))
    for minute in range(1, 121):
        restored.record(minute, 20, False, 5, True)
    assert restored.to_dict() == tracker.to_dict()


def test_tracker_seeds_ewmas_and_counts_nonoverlapping_windows():
    tracker = heating.AccuracyTracker()
    tracker.snapshot(0, 20, 0, 5, (0, 0, 0.01), 120)
    for minute in range(1, 361):
        tracker.record(minute, 20 + 0.005 * minute, False, 5, True)
        if minute % 15 == 0:
            tracker.snapshot(minute, 20 + 0.005 * minute, 0, 5, (0, 0, 0.01), 120)
        if minute == 60:
            assert tracker.mae60 == pytest.approx(0.3, abs=5e-08, rel=0)
            assert tracker.persistence_mae60 == pytest.approx(0.3, abs=5e-08, rel=0)
        if minute == 120:
            assert tracker.mae120 == pytest.approx(0.6, abs=5e-08, rel=0)
            assert tracker.persistence_mae120 == pytest.approx(0.6, abs=5e-08, rel=0)
    assert tracker.n120 == 17
    assert tracker.n120_independent == 3
    restored = heating.AccuracyTracker.from_dict(tracker.to_dict())
    assert restored.to_dict() == tracker.to_dict()


def test_covariance_is_symmetric_and_bounded():
    model = heating.SlabRLS(120)
    model.P = [[200, 1, 3], [1.1, 0.2, 0.1], [3.2, 0.11, 20]]
    for _ in range(16):
        model.add_minute(20, 20, True)
    for i, limit in enumerate((100, 0.1, 10)):
        assert model.P[i][i] <= limit * (1 + 1e-12)
        for j in range(3):
            assert model.P[i][j] == model.P[j][i]


def test_blocks_round_trip_per_candidate():
    model = heating.RoomModel()
    for index, candidate in enumerate(model.models.values()):
        candidate.blocks = index + 5
    restored = heating.RoomModel.from_dict(model.to_dict())
    assert [m.blocks for m in restored.models.values()] == list(range(5, 11))


def test_zero_on_rejected_when_below_target_without_reaching_it():
    with patch.object(heating, "forecast", return_value=[20.9] * 240):
        assert heating.plan_cycle(20.9, 0.9, 5, 21, (0.03, 0.0015, 0), 120)[0] == 10


def test_underestimating_model_forces_cold_guard():
    assert heating.plan_cycle(18, 0, 5, 21, (0, 0, 0), 120)[0] == 30


def test_overshoot_cost_weight_is_three():

    def values(temp, slab, outdoor, plan, theta, tau):
        return [21.2] * 240 if sum(plan[:30]) == 0 else [20.7] * 240

    with patch.object(heating, "forecast", side_effect=values):
        assert heating.plan_cycle(21, 0.9, 5, 21, (0.03, 0.0015, 0), 120)[0] == 10


def test_first_thirty_minutes_have_weight_point_two():

    def values(temp, slab, outdoor, plan, theta, tau):
        return [20] * 30 + [21] * 210 if sum(plan[:30]) == 0 else [20.8] * 240

    with patch.object(heating, "forecast", side_effect=values):
        assert heating.plan_cycle(21, 0.9, 5, 21, (0.03, 0.0015, 0), 120)[0] == 0


def test_planner_ties_choose_smallest_and_candidates_respect_minimum():
    candidates = []

    def values(temp, slab, outdoor, plan, theta, tau):
        candidates.append(sum(plan[:30]))
        return [21] * 240

    with patch.object(heating, "forecast", side_effect=values):
        assert heating.plan_cycle(21, 0.9, 5, 21, (0.03, 0.0015, 0), 120)[0] == 0
    assert candidates == [0, *range(10, 31)]


def test_tracker_uses_room_counter_at_half_minute_jitter():
    tracker = AccuracyTracker()
    for counter, now in enumerate((0.5, 1.5, 2.5, 3.5)):
        tracker.record(counter, 20, False, 5, True)
    indices = [row[0] for row in tracker.history]
    assert [b - a for a, b in itertools.pairwise(indices)] == [1, 1, 1]


def test_planner_candidates_respect_min_off():
    candidates = []

    def values(temp, slab, outdoor, plan, theta, tau):
        candidates.append(sum(plan[:30]))
        return [21] * 240

    with patch.object(heating, "forecast", side_effect=values):
        assert plan_cycle(21, 0.9, 5, 21, (0.03, 0.0015, 0), 120, min_off=10)[0] == 0
    assert candidates == [0, *range(10, 21), 30]


def test_steady_room_learns_its_heat_gain_within_two_days():
    """Live hallway, 2026-10-23: valve off, holding 24.2 C at 13.6 C outdoor.

    With v5's narrow prior the model still predicted 0.4 C/h of cooling
    after 537 blocks.
    """
    model = SlabRLS(120)
    for minute in range(2 * 1440 + 1):
        outdoor = 13.6 + 3 * math.sin(2 * math.pi * minute / 1440)
        model.step_slab(False, 1)
        model.add_minute(24.2, outdoor, True)
    _, beta, gamma = model.theta
    assert abs(gamma - beta * (24.2 - 13.6)) < 0.0005


def test_old_saved_covariance_is_widened_but_learning_kept():
    model = RoomModel()
    data = json.loads(json.dumps(model.to_dict()))
    del data["prior_version"]
    for key in data["P"]:
        data["P"][key] = [[1e-6, 0, 0], [0, 1e-9, 0], [0, 0, 1e-7]]
        data["theta"][key] = [0.021, 0.0008, 0.0018]
        data["blocks"][key] = 537
    restored = RoomModel.from_dict(data)
    for candidate in restored.models.values():
        assert candidate.P == [[1.0, 0, 0], [0, 1e-3, 0], [0, 0, 0.1]]
        assert candidate.theta == (0.021, 0.0008, 0.0018)
        assert candidate.blocks == 537
    again = RoomModel.from_dict(json.loads(json.dumps(restored.to_dict())))
    small = [[1e-6, 0, 0], [0, 1e-9, 0], [0, 0, 1e-7]]
    for candidate in restored.models.values():
        candidate.P = [row[:] for row in small]
    kept = RoomModel.from_dict(json.loads(json.dumps(restored.to_dict())))
    assert all(c.P == small for c in kept.models.values())
    assert again.to_dict()["prior_version"] == heating.PRIOR_VERSION
