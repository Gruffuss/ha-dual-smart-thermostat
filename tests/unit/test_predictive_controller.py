"""Pure per-minute regressions ported from the v5 app and review suites."""

from __future__ import annotations

from dataclasses import replace
import json
import math
from unittest.mock import patch

import pytest

from custom_components.dual_smart_thermostat.control import predictive as heating
from custom_components.dual_smart_thermostat.control.predictive import (
    MinuteInputs,
    PredictiveController,
    PredictiveSettings,
)
from custom_components.dual_smart_thermostat.control.predictive_model import (
    forecast,
    hold_duty,
    valve_plan,
)
from custom_components.dual_smart_thermostat.control.pwm import plan_pwm
from tests.unit.test_predictive_model import plant_step


def inputs(now=0, **kwargs):
    return replace(MinuteInputs(now, 23.8, 0, False, 24, True, True), **kwargs)


def controller(gate="auto", **kwargs):
    return PredictiveController(PredictiveSettings(30, 10, gate=gate, **kwargs))


class Driver:
    """The device's minute callback and PWM boundary, without HA or timers."""

    def __init__(self, ctl):
        self.ctl = ctl
        self.start = None
        self.on = 0
        self.valve = False

    def tick(self, inp):
        inp = replace(inp, valve_on=self.valve)
        settings = self.ctl.settings
        cycle = settings.cycle_minutes
        restart = self.ctl.minute(inp)
        elapsed = inp.now_min - self.start if self.start is not None else None
        if restart or elapsed is None or elapsed < 0 or elapsed + 0.05 >= cycle:
            on = self.ctl.plan(inp)
            # plan_pwm cannot express different on/off minima. Keep its raw
            # durations, then apply the same collapse rule to each side.
            pwm = plan_pwm(100 * on / cycle, 60 * cycle)
            self.on = pwm.on_seconds / 60
            if 0 < pwm.on_seconds < 60 * settings.min_on_minutes:
                self.on = 0
            elif 0 < pwm.off_seconds < 60 * settings.min_off_minutes:
                self.on = cycle
            self.start = inp.now_min
        self.valve = (
            inp.demand
            and inp.setpoint is not None
            and (inp.now_min - self.start + 0.05 < self.on)
        )
        return self.valve


def ready_gate():
    ctl = controller()
    ctl.tracker.n120 = 400
    ctl.tracker.n120_independent = 48
    ctl.tracker.mae120 = 0.2
    ctl.tracker.persistence_mae120 = 0.4
    for model in ctl.model.models.values():
        model.blocks = 672
        model.err = 0.000001
    return ctl


@pytest.mark.parametrize("minimum, expected", [(None, 10), (0, 0), (5, 5)])
def test_minimum_off_defaults_to_minimum_on(minimum, expected):
    assert (
        PredictiveSettings(30, 10, min_off_minutes=minimum).min_off_minutes == expected
    )


@pytest.mark.parametrize(
    "minimum, expected_on", [(None, 30), (0, 25), (5, 25), (6, 30)]
)
def test_driver_and_loss_learner_respect_asymmetric_minimum_off(minimum, expected_on):
    ctl = controller(
        "off", kp=0, ki=0, k_loss=100 * 25 / 30 / 24, min_off_minutes=minimum
    )
    driver = Driver(ctl)
    delivered = sum(driver.tick(inputs(minute, temp=24)) for minute in range(30))
    assert delivered == expected_on
    assert ctl.learner.valve_on_count == expected_on


@pytest.mark.parametrize("minimum", [0, 5])
def test_asymmetric_minimum_off_preserves_minimum_on(minimum):
    ctl = controller(
        "off", kp=0, ki=0, k_loss=100 * 5 / 30 / 24, min_off_minutes=minimum
    )
    driver = Driver(ctl)
    assert not driver.tick(inputs(temp=24))
    assert driver.on == 0
    assert ctl.bank.bank == pytest.approx(5)


def test_cold_floor_forces_cycle_and_demotes_for_24h():
    ctl = ready_gate()
    ctl.mode = "predictive"
    driver = Driver(ctl)
    assert driver.tick(inputs(temp=23.4))
    assert ctl.mode == "fallback"
    assert driver.on == 30
    assert ctl.demoted_until_min == 1440
    for minute in range(1, 30):
        assert driver.tick(inputs(minute, temp=24))
    ctl.minute(inputs(1439, temp=24))
    assert ctl.mode == "fallback"
    ctl.minute(inputs(1440, temp=24))
    assert ctl.mode == "predictive"


def test_cold_floor_still_yields_to_demand_fault_and_exercise():
    ctl = ready_gate()
    ctl.mode = "predictive"
    driver = Driver(ctl)
    assert driver.tick(inputs(temp=23.4))
    assert not driver.tick(inputs(1, demand=False))
    inp = inputs(2, temp=None)
    assert ctl.minute(inp)
    assert ctl.plan(inp) == pytest.approx(14.4)
    # Exercise owns the valve in the device: it withholds demand and validity.
    assert not driver.tick(inputs(3, temp=23.4, demand=False, valid=False))
    assert not ctl.tracker.pending


def test_forced_on_cannot_undo_cold_floor_demotion_after_reload():
    ctl = controller("on")
    ctl.minute(inputs(temp=23.4))
    saved = json.loads(json.dumps(ctl.to_dict()))
    restored = PredictiveController.from_dict(ctl.settings, saved, 120)
    assert restored.mode == "fallback"
    assert restored.demoted_until_min == pytest.approx(1320)
    restored.minute(inputs(1319, temp=24))
    assert restored.mode == "fallback"
    restored.minute(inputs(1320, temp=24))
    assert restored.mode == "predictive"


def test_gate_requires_skill_against_persistence():
    ctl = ready_gate()
    ctl.tracker.mae120 = 0.29
    ctl.minute(inputs())
    assert ctl.mode == "fallback"
    ctl.tracker.mae120 = 0.28
    assert ctl.minute(inputs(1))
    assert ctl.mode == "predictive"
    ctl.tracker.mae120 = 0.41
    assert ctl.minute(inputs(2))
    assert ctl.mode == "fallback"


def test_gate_requires_independent_windows_and_seven_days():
    ctl = ready_gate()
    ctl.tracker.n120_independent = 47
    ctl.minute(inputs())
    assert ctl.mode == "fallback"
    ctl.tracker.n120_independent = 48
    for model in ctl.model.models.values():
        model.blocks = 671
    ctl.minute(inputs(1))
    assert ctl.mode == "fallback"
    for model in ctl.model.models.values():
        model.blocks = 672
    ctl.minute(inputs(2))
    assert ctl.mode == "predictive"


def test_gate_checks_alpha_uncertainty():
    ctl = ready_gate()
    for model in ctl.model.models.values():
        model.err = 0.04
    ctl.minute(inputs())
    assert ctl.mode == "fallback"
    assert ctl.model.active().alpha_rel_sd() == pytest.approx(
        math.sqrt(0.04 * 0.001) / 0.02
    )
    for model in ctl.model.models.values():
        model.err = 0.000001
        model.theta = (0, 0.001, 0)
    ctl.minute(inputs(1))
    assert ctl.mode == "fallback"
    for model in ctl.model.models.values():
        model.theta = (0.02, 0.001, 0)
    ctl.minute(inputs(2))
    assert ctl.mode == "predictive"


def test_gate_absolute_mae_from_known_forecast_error():
    ctl = ready_gate()
    tracker = ctl.tracker
    tracker.n60 = tracker.n120 = 0
    tracker.snapshot(0, 24, 0, 5, (0, 0, 0.004), 120)
    for minute in range(1, 121):
        tracker.record(minute, 24, False, 5, True)
    tracker.persistence_mae120 = 1
    ctl.minute(inputs())
    assert tracker.mae120 == pytest.approx(0.48)
    assert ctl.mode == "fallback"


def test_gate_requires_validations_and_accuracy():
    ctl = controller()
    driver = Driver(ctl)
    temp, slab = 18, 0
    switches = 0
    for minute in range(7 * 1440 + 16):
        if minute:
            temp, slab = plant_step(temp, slab, 5, driver.valve, theta=(0.02, 0.001, 0))
        before = ctl.mode
        driver.tick(inputs(minute, temp=temp, outdoor=5))
        switches += ctl.mode != before
        if ctl.tracker.n120_independent < 48 or ctl.model.active().blocks < 672:
            assert ctl.mode == "fallback"
    assert ctl.tracker.n120_independent >= 48
    assert ctl.mode == "predictive"
    ctl.tracker.mae120 = 0.46
    before = ctl.mode
    driver.tick(inputs(minute + 1, temp=temp, outdoor=5))
    switches += ctl.mode != before
    assert ctl.mode == "fallback"
    assert switches == 2
    off = controller("off")
    off.tracker.n120 = 100
    off.minute(inputs())
    assert off.mode == "fallback"
    assert controller("on").mode == "predictive"


def test_restart_ages_slab_and_first_step_uses_elapsed_time():
    ctl = controller()
    for model in ctl.model.models.values():
        model.slab = 0.8
    restored = PredictiveController.from_dict(ctl.settings, ctl.to_dict(), 120)
    for tau, model in restored.model.models.items():
        assert model.slab == pytest.approx(0.8 * math.exp(-120 / tau))
    restored.minute(inputs(0.5, valve_on=False))
    for tau, model in restored.model.models.items():
        assert model.slab == pytest.approx(0.8 * math.exp(-120 / tau) * (1 - 0.5 / tau))


def test_round_trip():
    ctl = controller("on")
    driver = Driver(ctl)
    for minute in range(61):
        driver.tick(inputs(minute))
    ctl.tracker.n120, ctl.tracker.mae120 = 60, 0.12
    ctl.pi.integral = -20
    ctl.learner.k, ctl.learner.updates = 2.7, 4
    saved = json.loads(json.dumps(ctl.to_dict(), allow_nan=False))
    restored = PredictiveController.from_dict(PredictiveSettings(30, 10), saved, 0)
    assert restored.mode == "predictive"
    assert restored.model.to_dict() == ctl.model.to_dict()
    assert restored.tracker.to_dict() == ctl.tracker.to_dict()
    assert restored.pi.integral == -20
    assert restored.learner.k == 2.7
    assert restored.learner.updates == 4
    assert not restored.tracker.history
    assert not restored.tracker.pending


def test_v4_file_refused():
    with pytest.raises(ValueError, match="v5|model"):
        PredictiveController.from_appdaemon_room(
            PredictiveSettings(30, 10),
            {"k_loss": 3.5, "integral": -8, "k_updates": 2},
            0,
        )


def test_predictive_freezes_unsaturated_integral():
    ctl = controller("on")
    ctl.pi.integral = 12
    driver = Driver(ctl)
    for minute in range(5):
        driver.tick(inputs(minute, outdoor=5))
    assert ctl.pi.integral == 12
    assert ctl.learner.updates == 0
    assert ctl.bank.bank == 0


def test_predictive_loss_update_transfers_integral():
    ctl = controller("on")
    ctl.learner.window = 1
    ctl.pi.integral = 12
    before = ctl.learner.k * 24 + 0.05 * 12
    Driver(ctl).tick(inputs(temp=24))
    assert ctl.learner.updates == 1
    assert ctl.learner.k * 24 + 0.05 * ctl.pi.integral == pytest.approx(before)


def test_ac_cooldown_excludes_model_and_loss_learner():
    ctl = controller()
    driver = Driver(ctl)
    # Device supplies False for active AC and its following 30 minutes.
    for minute in range(31):
        driver.tick(inputs(minute, temp=24, valid=False))
    assert ctl.model.active().blocks == 0
    assert not ctl.tracker.pending
    assert ctl.learner.n == 0
    for minute in range(31, 47):
        driver.tick(inputs(minute, temp=24))
    assert ctl.model.active().blocks == 1


def test_window_and_cooldown_invalidate_model_and_tracker():
    ctl = controller()
    driver = Driver(ctl)
    for minute in range(14):
        driver.tick(inputs(minute))
    for minute in range(14, 45):  # 16 open, then 15 cooldown
        driver.tick(inputs(minute, valid=False))
        assert not ctl.tracker.history[-1][-1]
    assert ctl.model.active().blocks == 0
    assert not ctl.tracker.pending
    for minute in range(45, 61):
        driver.tick(inputs(minute))
    assert ctl.model.active().blocks == 1


def test_legacy_scalar_blocks_and_unpaired_accuracy_migrate():
    ctl = controller()
    saved = ctl.to_dict()
    saved["blocks"] = 99
    saved["n60"], saved["n120"], saved["n120_independent"] = 100, 60, 48
    del saved["persistence_mae120"]
    restored = PredictiveController.from_dict(ctl.settings, saved, 0)
    assert all(model.blocks == 99 for model in restored.model.models.values())
    assert (
        restored.tracker.n60,
        restored.tracker.n120,
        restored.tracker.n120_independent,
    ) == (0, 0, 0)


def test_predictive_without_outdoor_uses_fallback():
    ctl = controller("on")
    ctl.pi.integral = 12
    Driver(ctl).tick(inputs(outdoor=None))
    assert ctl.pi.integral == pytest.approx(12.2)
    assert ctl.pi.output == pytest.approx(13.61)
    assert ctl.forecasts(inputs(outdoor=None), 0, 0) == dict.fromkeys(
        (30, 60, 120, 180, 240)
    )


@pytest.mark.parametrize("beta", [0.0015, 0, 1 / 120])
def test_cached_forecasts_match_simulation_with_current_readings(beta):
    ctl = controller()
    model = ctl.model.active()
    model.theta = (0.03, beta, 0.001)
    model.slab = 0.8
    hold = round(hold_duty(model.theta, 21, 5) * 30)
    hold = hold if hold >= 10 else 0
    full_plan = valve_plan(10, hold, 30, 270)
    with patch.object(heating, "forecast", wraps=forecast) as simulate:
        for minute in range(30):
            model.slab = 0.8 - minute / 100
            inp = inputs(
                minute, temp=20 + minute / 50, outdoor=5 + minute / 30, setpoint=21
            )
            expected = forecast(
                inp.temp,
                model.slab,
                inp.outdoor,
                full_plan[minute : minute + 240],
                model.theta,
                model.tau,
            )
            values = ctl.forecasts(inp, minute, 10)
            for horizon in (30, 60, 120, 180, 240):
                assert values[horizon] == round(expected[horizon - 1], 1)
        assert simulate.call_count == 1


def test_predictive_plans_only_at_cycle_boundaries():
    ctl = controller("on")
    driver = Driver(ctl)
    with patch.object(heating, "plan_cycle", wraps=heating.plan_cycle) as planner:
        for minute in range(90):
            driver.tick(inputs(minute))
        assert planner.call_count == 3
        assert all(call.kwargs["min_off"] == 10 for call in planner.call_args_list)


def test_demand_off_still_trains_model_and_gaps_invalidate():
    ctl = controller()
    ctl.pi.integral, ctl.bank.bank = 12, 9
    driver = Driver(ctl)
    for minute in range(16):
        assert not driver.tick(inputs(minute, demand=False))
    assert ctl.model.active().blocks == 1
    assert ctl.bank.bank == 0
    assert ctl.pi.integral == 12
    assert ctl.pi.output == 0
    driver.tick(inputs(21, demand=False))
    assert not ctl.tracker.pending
    assert ctl.model.active().previous is None


def test_setpoint_change_restarts_cycle_without_resetting_integral():
    ctl = controller()
    inp = inputs()
    ctl.minute(inp)
    ctl.plan(inp)
    before = ctl.pi.integral
    assert ctl.minute(inputs(1, setpoint=24.2))
    assert ctl.pi.integral == pytest.approx(before + 0.4)
    assert ctl.learner.n == 0


def test_controller_tracker_uses_room_counter_at_half_minute_jitter():
    ctl = controller()
    for now in (0.5, 1.5, 2.5, 3.5):
        ctl.minute(inputs(now))
    indices = [row[0] for row in ctl.tracker.history]
    assert [b - a for a, b in zip(indices, indices[1:])] == [1, 1, 1]


def test_setpoint_restart_between_ticks_uses_new_input_without_integrating_twice():
    ctl = controller("off", k_loss=0)
    initial = inputs(temp=24)
    assert ctl.plan(initial) == 0
    assert ctl.pi.integral == 0
    assert ctl.plan(replace(initial, setpoint=25)) == pytest.approx(19.5)
    assert ctl.pi.integral == 0
    assert len(ctl.tracker.history) == 1
    assert ctl.plan(replace(initial, demand=False)) == 0
    assert ctl.pi.output == 0
    assert ctl.plan(replace(initial, setpoint=25)) == pytest.approx(19.5)
    assert ctl.pi.integral == 0


def test_invalid_saved_state_starts_fresh_with_warning(caplog):
    ctl = controller()
    saved = ctl.to_dict()
    saved["theta"]["120"][0] = float("nan")
    restored = PredictiveController.from_dict(ctl.settings, saved, 0)
    assert restored.model.active().theta == (0.02, 0.001, 0)
    assert "invalid saved" in caplog.text


def appdaemon_v5_room():
    """Room copied from a state file written by the unmodified final v5 app.

    Reproduction: 61 minute ticks, T=23.8, outdoor=0, target=24, forced on,
    kp=65, ki=.05; clock starts at 2026-10-18 04:00 UTC. No live state used.
    """
    return json.loads(
        r"""{"k_loss":2.0,"integral":0.0,"k_updates":0,"last_k_update":null,"slab":{"60":0.6352076892466554,"90":0.48849506006109084,"120":0.3947386799439596,"150":0.33057710535391827,"180":0.28413430251985317,"240":0.2216058664038915},"theta":{"60":[0.0200222729559457,0.0009984362758737144,6.570269438175664e-06],"90":[0.020018904397525173,0.000998243313167598,7.381037110933254e-06],"120":[0.020016126419256493,0.0009981307789314357,7.853870035984144e-06],"150":[0.020013973401531236,0.0009980573583814,8.16235974201684e-06],"180":[0.02001229440648591,0.0009980057541059968,8.37918442858585e-06],"240":[0.02000988056416364,0.0009979380785878871,8.663535345011215e-06]},"P":{"60":[[0.001019544213306655,3.689021437046802e-08,-1.5500090071625213e-07],[3.689021437046802e-08,1.017919324280277e-06,9.803351979180486e-09],[-1.5500090071625213e-07,9.803351979180486e-09,0.00010198406165059784]],"90":[[0.001019863011288426,2.707747451083734e-08,-1.1377090130603926e-07],[2.707747451083734e-08,1.0179187083898926e-06,9.805939753904839e-09],[-1.1377090130603926e-07,9.805939753904839e-09,0.0001019840507775948]],"120":[[0.0010200076526389085,2.1350408166365103e-08,-8.970759733766846e-08],[2.1350408166365103e-08,1.0179184363785206e-06,9.807082658829497e-09],[-8.970759733766846e-08,9.807082658829497e-09,0.00010198404597547324]],"150":[[0.001020084733260914,1.7612173962039486e-08,-7.400073093293899e-08],[1.7612173962039486e-08,1.0179182935612625e-06,9.807682731341636e-09],[-7.400073093293899e-08,9.807682731341636e-09,0.00010198404345416018]],"180":[[0.0010201304968088657,1.4983904073494077e-08,-6.295758014073139e-08],[1.4983904073494077e-08,1.0179182095669044e-06,9.808035648812504e-09],[-6.295758014073139e-08,9.808035648812504e-09,0.00010198404197131366]],"240":[[0.0010201797515725867,1.1536080081956194e-08,-4.847092471410164e-08],[1.1536080081956194e-08,1.0179181199287032e-06,9.808412279910494e-09],[-4.847092471410164e-08,9.808412279910494e-09,0.00010198404038883005]]},"err":{"60":0.00042072489335662796,"90":0.0004604403076033419,"120":0.00048303090079231353,"150":0.0004976446677288635,"180":0.0005078818918749872,"240":0.000521286841572268},"blocks":{"60":4,"90":4,"120":4,"150":4,"180":4,"240":4},"active_tau":60,"mae60":1.1314139789584985,"mae120":0.0,"n60":1,"n120":0,"persistence_mae60":0.0,"persistence_mae120":0.0,"n120_independent":0,"mode":"predictive","model_saved_at":"2026-10-18T05:00:00+00:00","demoted_until":null}"""
    )


def test_appdaemon_v5_room_imports():
    room = appdaemon_v5_room()
    ctl = PredictiveController.from_appdaemon_room(
        PredictiveSettings(30, 10), room, 120
    )
    assert ctl.mode == "predictive"
    assert ctl.pi.integral == room["integral"]
    assert ctl.learner.k == room["k_loss"]
    assert ctl.learner.updates == room["k_updates"]
    assert ctl.tracker.to_dict() == {key: room[key] for key in ctl.tracker.to_dict()}
    for tau, model in ctl.model.models.items():
        assert model.slab == pytest.approx(
            room["slab"][str(tau)] * math.exp(-120 / tau)
        )
        assert list(model.theta) == room["theta"][str(tau)]
        assert model.blocks == room["blocks"][str(tau)]
    assert not ctl.tracker.history
