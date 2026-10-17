"""Measure the always-on solar fixture, PI against predictive auto.

Replicates ``simulate("off"|"auto", solar=True)`` from
``tests/unit/test_predictive_closed_loop.py`` (production actuator, min_on =
min_off = 10 min) and prints both side by side, with the minutes below
sp - 0.3 and sp - 0.7 split by the auto run's gate segments.

Each variant changes one thing, by patching the pure control code in this
process only; the oracles are test-only and need no production change. A
"+v5" suffix selects v5's actuator (min_off 0) and v5's gate (it also
demotes on persistence), "+v5gate" v5's gate alone. ``--seeds`` reruns the
fixture with other noise seeds as a diagnostic; acceptance uses 32 seeds
(0-31), with the margin applied to the mean of auto minus PI.

Run from the repository root with PYTHONPATH set to it:
    python tools/predictive_coldtime.py [variant ...]
    python tools/predictive_coldtime.py --seeds 0-255 [variant ...]
Variants: baseline and the keys of VARIANTS and SETTINGS below.
Results: docs/plans/2026-10-17-predictive-heater-mode-results.md.
"""

from __future__ import annotations

import itertools
import math
import random
import sys

from custom_components.dual_smart_thermostat.control import predictive, predictive_model
from custom_components.dual_smart_thermostat.control.predictive import (
    MinuteInputs,
    PredictiveController,
    PredictiveSettings,
)
from tests.unit.test_predictive_controller import Driver
from tests.unit.test_predictive_model import plant_step

THETA = (0.03, 0.0015, 0)
SETPOINT = 21
DAYS = 10


def true_outdoor(minute):
    return 8 + 5 * math.sin(2 * math.pi * minute / 1440)


# The simulated minute, for test-only oracles that look ahead of the inputs.
NOW = [0]


def simulate(mode, seed=42, **settings):
    ctl = PredictiveController(PredictiveSettings(30, 10, gate=mode, **settings))
    driver = Driver(ctl)
    temp, slab = 21, 0.8
    rng = random.Random(seed)
    transitions, temperatures, valves = [], [], []
    for minute in range(DAYS * 1440):
        NOW[0] = minute
        outdoor = true_outdoor(minute)
        reading = round(temp + rng.gauss(0, 0.05), 1)
        before = ctl.mode
        valve = driver.tick(
            MinuteInputs(minute, reading, outdoor, driver.valve, SETPOINT, True, True)
        )
        if before != ctl.mode:
            transitions.append(
                (minute, ctl.mode, ctl.tracker.mae120, ctl.tracker.persistence_mae120)
            )
        valves.append(valve)
        temp, slab = plant_step(temp, slab, outdoor, valve, theta=THETA)
        time_of_day = minute % 1440
        if 600 <= time_of_day <= 960:
            temp += 0.01 * math.sin(math.pi * (time_of_day - 600) / 360)
        temperatures.append(temp)
    return {
        "ctl": ctl,
        "temps": temperatures,
        "valves": valves,
        "transitions": transitions,
        "below_03": sum(t < SETPOINT - 0.3 for t in temperatures),
        "below_07": sum(t < SETPOINT - 0.7 for t in temperatures),
        "min_temp": min(temperatures),
        "overshoot": max(0, max(temperatures) - SETPOINT),
    }


def margin_ok(pi, auto):
    return (
        auto["below_03"] <= pi["below_03"] + 60
        and auto["below_07"] <= pi["below_07"] + 60
        and auto["min_temp"] >= pi["min_temp"] - 0.1
        and auto["overshoot"] <= pi["overshoot"] + 0.3
    )


def report(pi, auto):
    print(f"{'':24}{'PI':>12}{'auto':>12}{'allowed':>12}")
    for key, allowed in (
        ("below_03", pi["below_03"] + 60),
        ("below_07", pi["below_07"] + 60),
    ):
        print(f"{key:24}{pi[key]:>12}{auto[key]:>12}{allowed:>12}")
    print(
        f"{'min_temp':24}{pi['min_temp']:>12.6f}{auto['min_temp']:>12.6f}"
        f"{pi['min_temp'] - 0.1:>12.6f}"
    )
    print(
        f"{'overshoot':24}{pi['overshoot']:>12.6f}{auto['overshoot']:>12.6f}"
        f"{pi['overshoot'] + 0.3:>12.6f}"
    )
    print("margin met:", margin_ok(pi, auto))
    for minute, mode, mae, persistence in auto["transitions"]:
        print(
            f"  {minute:>6} -> {mode:10} mae120={mae:.4f} "
            f"persistence_mae120={persistence:.4f}"
        )
    edges = [0, *(m for m, *_ in auto["transitions"]), len(auto["temps"])]
    diverge = next(
        (i for i, (a, b) in enumerate(zip(pi["temps"], auto["temps"])) if a != b),
        None,
    )
    print("first divergence minute:", diverge)
    print(
        "segment            mode         PI<20.7 auto<20.7  PI<20.3 auto<20.3  on PI/auto"
    )
    modes = ["fallback", *(mode for _, mode, *_ in auto["transitions"])]
    for (start, end), mode in zip(itertools.pairwise(edges), modes):
        row = [
            sum(t < limit for t in run["temps"][start:end])
            for limit in (20.7, 20.3)
            for run in (pi, auto)
        ]
        on = [sum(run["valves"][start:end]) for run in (pi, auto)]
        print(
            f"  {start:>6}-{end:<6}   {mode:10} {row[0]:>8} {row[1]:>9}"
            f" {row[2]:>8} {row[3]:>9}  {on[0]}/{on[1]}"
        )
    active = auto["ctl"].model.active()
    print(
        f"auto model: tau={active.tau} theta={tuple(round(v, 6) for v in active.theta)}"
        f" blocks={active.blocks} alpha_rel_sd={active.alpha_rel_sd():.4f}"
    )


def _patch(module, name, replacement):
    original = getattr(module, name)
    setattr(module, name, replacement(original))
    return lambda: setattr(module, name, original)


def oracle_sequence(start, steps):
    return tuple(true_outdoor(start + i) for i in range(steps))


def sequence_forecast():
    """Let forecast and hold_duty take a per-minute outdoor tuple (oracle only).

    A scalar outdoor goes to the unchanged v5 functions. A tuple gives the
    outdoor of each forecast minute, element 0 being now; the hold duty uses
    element 0, the outdoor the planner holds constant today.
    """

    def wrap_forecast(original):
        def forecast(temp, slab, outdoor, plan, theta, tau):
            if not isinstance(outdoor, tuple):
                return original(temp, slab, outdoor, plan, theta, tau)
            alpha, beta, gamma = theta
            values = []
            for valve, out in zip(plan, outdoor, strict=False):
                slab = predictive_model.clamp(slab + (valve - slab) / tau, 0, 1)
                temp += alpha * slab - beta * (temp - out) + gamma
                values.append(temp)
            assert len(values) == len(plan), "outdoor sequence too short"
            return values

        return forecast

    def wrap_hold(original):
        def hold_duty(theta, setpoint, outdoor):
            now = outdoor[0] if isinstance(outdoor, tuple) else outdoor
            return original(theta, setpoint, now)

        return hold_duty

    undos = [
        _patch(predictive_model, "forecast", wrap_forecast),
        _patch(predictive_model, "hold_duty", wrap_hold),
    ]
    return lambda: [undo() for undo in reversed(undos)]


def planner_oracle():
    """Candidate 1 ceiling: the planner sees the fixture's true outdoor."""

    def wrap(original):
        def plan_cycle(temp, slab, outdoor, *args, horizon=240, **kwargs):
            return original(
                temp, slab, oracle_sequence(NOW[0], horizon), *args, **kwargs
            )

        return plan_cycle

    undo_sequence = sequence_forecast()
    undo_plan = _patch(predictive, "plan_cycle", wrap)
    return lambda: (undo_plan(), undo_sequence())


def tracker_oracle():
    """The tracker re-simulates each snapshot with the true outdoor."""

    def wrap(original):
        def snapshot(self, now_min, temp, slab, outdoor, theta, tau):
            return original(
                self, now_min, temp, slab, oracle_sequence(now_min, 120), theta, tau
            )

        return snapshot

    undo_sequence = sequence_forecast()
    undo_snapshot = _patch(predictive_model.AccuracyTracker, "snapshot", wrap)
    return lambda: (undo_snapshot(), undo_sequence())


def both(*setups):
    def setup():
        undos = [s() for s in setups]
        return lambda: [u() for u in reversed(undos)]

    return setup


def hold_oracle():
    """Candidate 2: the planner's hold duty is the plant's no-solar steady duty."""

    def wrap(original):
        def hold_duty(theta, setpoint, outdoor):
            return predictive_model.clamp(
                THETA[1] * (setpoint - outdoor) / THETA[0], 0, 1
            )

        return hold_duty

    return _patch(predictive_model, "hold_duty", wrap)


def no_loss_learning():
    """Candidate 2: no loss-learner window ever completes (k stays at k_loss)."""

    def wrap(original):
        def sample(self, valid, setpoint, room, outdoor, valve_on):
            self.reset("learning disabled")

        return sample

    return _patch(predictive.LossLearner, "sample", wrap)


def no_settings():
    return None


def pre_fix_pwm():
    """Reinstate the float collapse that plan_pwm had before its fix.

    100 * 20 / 30 percent of 1800 s is 1200.0000000000002 s, leaving
    599.9999999999998 s off, which a strict comparison with the 600 s minimum
    collapses to a full 30-minute cycle. Reproduces runs made before the fix;
    the earlier "exact PWM" rows are the fix itself.
    """
    from tests.unit import test_predictive_controller

    def wrap(original):
        def plan_pwm(
            duty_pct, cycle_seconds, min_segment_seconds=0.0, min_off_seconds=None
        ):
            raw = original(duty_pct, cycle_seconds)
            min_off = (
                min_segment_seconds if min_off_seconds is None else min_off_seconds
            )
            if 0 < raw.on_seconds < min_segment_seconds:
                return type(raw)(0.0, cycle_seconds)
            if 0 < raw.off_seconds < min_off:
                return type(raw)(cycle_seconds, 0.0)
            return raw

        return plan_pwm

    undos = [
        _patch(predictive, "plan_pwm", wrap),
        _patch(test_predictive_controller, "plan_pwm", wrap),
    ]
    return lambda: [undo() for undo in undos]


def bank_counts_full_cycle():
    """Other cause: the duty bank deducts the full cycle the actuator runs.

    With min_off, a banked pulse longer than cycle - min_off is executed as the
    full cycle, but DutyBank deducts only the planned minutes, so the extra
    minutes are delivered and never charged. Here the bank deducts the full
    cycle and carries the over-delivery forward as a negative balance.
    """

    def wrap(original):
        def plan(self, output_pct):
            cycle = self.cycle_time
            self.bank = min(
                cycle, self.bank + predictive.clamp(output_pct) / 100 * cycle
            )
            on = min(self.bank, cycle) if self.bank >= self.min_on_time else 0.0
            min_off = getattr(self, "min_off_time", 0)
            if 0 < on < cycle and cycle - on < min_off:
                on = cycle
            self.bank -= on
            return on

        return plan

    def wrap_init(original):
        def __init__(self, settings):
            original(self, settings)
            self.bank.min_off_time = settings.min_off_minutes

        return __init__

    undos = [
        _patch(predictive.DutyBank, "plan", wrap),
        _patch(PredictiveController, "__init__", wrap_init),
    ]
    return lambda: [undo() for undo in reversed(undos)]


def bumpless_demotion():
    """Other cause: at predictive -> fallback, back-calculate the PI integral.

    The integral is set so that ff + kp * error + ki * integral equals the duty
    predictive was running, instead of resuming the value frozen at promotion.
    """

    def wrap(original):
        def _switch_mode(self, mode):
            inp = self._last_inputs
            if (
                mode == "fallback"
                and self.mode == "predictive"
                and inp is not None
                and inp.temp is not None
                and inp.setpoint is not None
                and self.pi.ki > 0
            ):
                pi = self.pi
                error = inp.setpoint - inp.temp
                ff = self.learner.feedforward(inp.setpoint, inp.outdoor)
                duty = 100 * self._on_minutes / self.bank.cycle_time
                limit = pi.i_max / pi.ki
                pi.integral = predictive.clamp(
                    (duty - ff - pi.kp * error) / pi.ki, -limit, limit
                )
            return original(self, mode)

        return _switch_mode

    return _patch(PredictiveController, "_switch_mode", wrap)


def integral_transplant():
    """Causal test (oracle): at demotion, take the PI-only run's integral.

    run() simulates PI before auto, so the PI run records its integral per
    minute and the auto run copies it at every predictive -> fallback switch.
    """
    recorded = {}

    def wrap_minute(original):
        def minute(self, inp):
            result = original(self, inp)
            if self.settings.gate == "off":
                recorded[inp.now_min] = self.pi.integral
            return result

        return minute

    def wrap_switch(original):
        def _switch_mode(self, mode):
            if (
                self.settings.gate == "auto"
                and self.mode == "predictive"
                and mode == "fallback"
            ):
                self.pi.integral = recorded[self._now_min]
            return original(self, mode)

        return _switch_mode

    undos = [
        _patch(PredictiveController, "minute", wrap_minute),
        _patch(PredictiveController, "_switch_mode", wrap_switch),
    ]
    return lambda: [undo() for undo in reversed(undos)]


def demote_on_absolute_error_only():
    """Other cause: drop the 'mae120 > persistence_mae120' demotion clause.

    While predictive holds the room steady, |T_now - T_snapshot| shrinks, so
    the persistence baseline improves with the controller it judges. Since
    the owner's ruling this is production's gate; the variant now only
    changes runs with "+v5" or "+v5gate".
    """

    def wrap(original):
        def _update_mode(self, now):
            if self.mode == "predictive" and self.settings.gate == "auto":
                cooling_down = (
                    self.demoted_until_min is not None and now < self.demoted_until_min
                )
                if not cooling_down and self.tracker.mae120 <= self.settings.mae_off:
                    return False
            return original(self, now)

        return _update_mode

    return _patch(PredictiveController, "_update_mode", wrap)


def integrate_while_predictive():
    """Other cause: the PI keeps integrating its error while predictive runs."""

    def wrap(original):
        def minute(self, inp):
            new_minute = self._processed_at != inp.now_min
            result = original(self, inp)
            temp, setpoint = inp.temp, inp.setpoint
            if (
                new_minute
                and self.mode == "predictive"
                and inp.demand
                and temp is not None
                and setpoint is not None
                and inp.outdoor is not None
            ):
                output = self.pi.output
                ff = self.learner.feedforward(setpoint, inp.outdoor)
                self.pi.update(setpoint - temp, 1, ff)
                self.pi.freeze_output(output)
            return result

        return minute

    return _patch(PredictiveController, "minute", wrap)


# Variants that only change controller settings, applied to PI and auto alike.
SETTINGS = {
    "c2_k_oracle": {"k_loss": 100 * THETA[1] / THETA[0]},
    "c3_kp_40": {"kp": 40.0},
    "c3_kp_100": {"kp": 100.0},
    "c3_ki_0025": {"ki": 0.025},
    "c3_ki_01": {"ki": 0.1},
}

VARIANTS = {
    "c1_planner_oracle": planner_oracle,
    "c1_planner_tracker_oracle": both(planner_oracle, tracker_oracle),
    "c2_hold_oracle": hold_oracle,
    "c2_k_oracle": no_loss_learning,
    **{name: no_settings for name in SETTINGS if name.startswith("c3_")},
    "x_pre_fix_pwm": pre_fix_pwm,
    "x_bank_full_cycle": bank_counts_full_cycle,
    "x_bumpless_demotion": bumpless_demotion,
    "x_integrate_while_predictive": integrate_while_predictive,
    "x_integral_transplant": integral_transplant,
    "x_demote_absolute_only": demote_on_absolute_error_only,
}


def run(name, seed=42):
    """Run PI and auto, with v5's actuator and gate on a "+v5" suffix.

    "+v5gate" selects v5's gate alone (persistence demotion, production's
    gate until the owner's ruling of 2026-10-17).
    """
    base, _, suffix = name.partition("+")
    setup = VARIANTS.get(base)
    if setup is None and base != "baseline":
        raise SystemExit(f"unknown variant {name}")
    undo = setup() if setup else None
    try:
        settings = dict(SETTINGS.get(base, {}))
        if suffix in ("v5", "v5gate"):
            settings["persistence_demotion"] = True
        if suffix == "v5":
            settings["min_off_minutes"] = 0
        elif suffix not in ("", "v5gate"):
            raise SystemExit(f"unknown suffix {suffix}")
        return simulate("off", seed, **settings), simulate("auto", seed, **settings)
    finally:
        if undo:
            undo()


def seed_row(job):
    """One seed of a sweep, reduced to numbers so it crosses processes."""
    name, seed = job
    pi, auto = run(name, seed)
    return (
        seed,
        pi["below_03"],
        auto["below_03"],
        pi["below_07"],
        auto["below_07"],
        auto["min_temp"] - pi["min_temp"],
        auto["overshoot"] - pi["overshoot"],
        segment_differences(pi, auto),
        margin_ok(pi, auto),
    )


def segment_differences(pi, auto):
    """Auto minus PI cold minutes inside auto's predictive and later fallback spans."""
    transitions = auto["transitions"]
    edges = [minute for minute, *_ in transitions] + [len(auto["temps"])]
    result = {"predictive": [0, 0], "fallback": [0, 0]}
    for (start, end), (_, mode, *_) in zip(itertools.pairwise(edges), transitions):
        for index, limit in enumerate((SETPOINT - 0.3, SETPOINT - 0.7)):
            result[mode][index] += sum(
                t < limit for t in auto["temps"][start:end]
            ) - sum(t < limit for t in pi["temps"][start:end])
    return result


def mean_se(values):
    mean = sum(values) / len(values)
    if len(values) < 2:
        return mean, math.nan
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return mean, math.sqrt(variance / len(values))


def sweep(name, first, last, jobs):
    from multiprocessing import Pool

    print(f"\n=== {name}, seeds {first}-{last} (diagnostic only) ===")
    print("seed  PI<20.7 auto<20.7  PI<20.3 auto<20.3   dminT  dovershoot  met")
    with Pool(jobs) as pool:
        rows = pool.map(seed_row, [(name, s) for s in range(first, last + 1)])
    for seed, p03, a03, p07, a07, dmin, dover, _, met in rows[:16]:
        print(
            f"{seed:>4} {p03:>8} {a03:>9} {p07:>8} {a07:>9}"
            f" {dmin:>7.3f} {dover:>11.3f}  {met}"
        )
    if len(rows) > 16:
        print(f"  ... {len(rows) - 16} more seeds")
    for label, index in (("<20.7", 1), ("<20.3", 3)):
        pi_mean, _ = mean_se([row[index] for row in rows])
        auto_mean, _ = mean_se([row[index + 1] for row in rows])
        mean, se = mean_se([row[index + 1] - row[index] for row in rows])
        print(
            f"{label}: PI mean {pi_mean:.1f}, auto mean {auto_mean:.1f};"
            f" auto-PI mean {mean:+.1f} (standard error {se:.1f})"
        )
    for mode in ("predictive", "fallback"):
        for index, label in enumerate(("<20.7", "<20.3")):
            mean, se = mean_se([row[7][mode][index] for row in rows])
            print(
                f"  inside auto's {mode} spans after promotion, auto-PI {label}:"
                f" mean {mean:+.1f} (standard error {se:.1f})"
            )
    for label, index in (("min T", 5), ("overshoot", 6)):
        mean, se = mean_se([row[index] for row in rows])
        print(f"auto-PI {label}: mean {mean:+.4f} (standard error {se:.4f})")
    print(f"seeds meeting the margin: {sum(row[-1] for row in rows)}/{len(rows)}")


def main(argv):
    """``variant ...`` reports the fixture; ``--seeds a-b variant`` sweeps seeds."""
    if argv[:1] == ["--seeds"]:
        first, last = map(int, argv[1].split("-"))
        for name in argv[2:] or ["baseline"]:
            sweep(name, first, last, 16)
        return
    for name in argv or ["baseline"]:
        pi, auto = run(name)
        print(f"\n=== {name} ===")
        report(pi, auto)


if __name__ == "__main__":
    main(sys.argv[1:])
