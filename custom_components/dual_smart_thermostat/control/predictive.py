"""Per-minute v5 predictive decisions, independent of HA and valve services.

The caller supplies learning validity, including the 15-minute window and
30-minute AC cooldowns. Exercise owns the actuator in the device and withholds
demand as well as validity. Invalid AC minutes alone do not suppress heating.
Call minute before plan at a PWM boundary; plan also accepts the first sample
itself. The controller integrates PI once per minute, never once per cycle.
A plan never counts a minute: the device's PWM boundary reads the clock apart
from the minute tick, so plan() works from the last processed minute with the
current readings, whatever its now_min.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import logging
import math

from .pi_fallback import (
    DutyBank,
    LossLearner,
    PIController,
    clamp,
    finite_float,
    transfer_integral,
)
from .predictive_model import (
    AccuracyTracker,
    RoomModel,
    forecast,
    hold_duty,
    plan_cycle,
    valve_plan,
)
from .pwm import plan_pwm

_LOGGER = logging.getLogger(__name__)


@dataclass
class PredictiveSettings:
    """PWM timing and the v5 accuracy gate and fallback defaults."""

    cycle_minutes: int
    min_on_minutes: int
    gate: str = "auto"
    mae_on: float = 0.3
    mae_off: float = 0.45
    min_validations: int = 48
    min_blocks: int = 672
    kp: float = 65.0
    ki: float = 0.05
    k_loss: float = 2.0
    min_off_minutes: int | None = None
    # v5 also demoted when mae120 > persistence_mae120. The owner dropped that
    # (2026-10-17): a room held steady by predictive shrinks the persistence
    # baseline it is judged against. True only reproduces v5 in the
    # equivalence tests; it is not a configuration option.
    persistence_demotion: bool = False

    def __post_init__(self) -> None:
        """Production protects both segments; zero reproduces the v5 actuator."""
        if self.min_off_minutes is None:
            self.min_off_minutes = self.min_on_minutes


@dataclass
class MinuteInputs:
    """One reading; valve_on describes the interval that just ended.

    now_min is elapsed monotonic minutes. After restore its origin is zero at
    load time, so the first tick includes time elapsed since loading the slab.
    The device retains last-known outdoor; None means none has ever been known.
    """

    now_min: int
    temp: float | None
    outdoor: float | None
    valve_on: bool
    setpoint: float | None
    demand: bool
    valid: bool


class PredictiveController:
    """Learning, gating and cycle policy; no actuator or sensor side effects."""

    def __init__(self, settings: PredictiveSettings) -> None:
        self.settings = settings
        self.model = RoomModel()
        self.tracker = AccuracyTracker()
        self.pi = PIController(settings.kp, settings.ki)
        self.bank = DutyBank(settings.cycle_minutes, settings.min_on_minutes)
        self.learner = LossLearner(clamp(settings.k_loss, 0, 10))
        self.mode = "predictive" if settings.gate == "on" else "fallback"
        self.demoted_until_min: int | None = None
        self._model_last_update = None
        self._model_minute = -1
        self._snapshot_at = None
        self._last_update = None
        self._processed_at = None
        self._last_inputs = None
        self._learned_at = None
        self._now_min = 0
        self._cycle_start = None
        self._on_minutes = 0.0
        self._setpoint = None
        self._demand = False
        self._fault = False
        self._seen_temp = False
        self._restart = False
        self._cold_floor = False
        self._forecast_cache = None

    def _switch_mode(self, mode):
        if mode == self.mode:
            return False
        self.mode = mode
        self._cycle_start = None
        self.bank.clear()
        return True

    def _update_mode(self, now):
        tracker, model, settings = self.tracker, self.model.active(), self.settings
        mode = self.mode
        cooling_down = (
            self.demoted_until_min is not None and now < self.demoted_until_min
        )
        if cooling_down or settings.gate == "off":
            mode = "fallback"
        elif settings.gate == "on":
            mode = "predictive"
        elif self.mode == "fallback" and (
            tracker.n120_independent >= settings.min_validations
            and model.blocks >= settings.min_blocks
            and model.alpha_rel_sd() <= 0.25
            and tracker.mae120 <= settings.mae_on
            and tracker.mae120 <= 0.7 * tracker.persistence_mae120 + 1e-12
        ):
            mode = "predictive"
        elif self.mode == "predictive" and (
            tracker.mae120 > settings.mae_off
            or (
                settings.persistence_demotion
                and tracker.mae120 > tracker.persistence_mae120
            )
        ):
            mode = "fallback"
        return self._switch_mode(mode)

    def _idle(self):
        self.bank.clear()
        self._cycle_start = None
        self._on_minutes = 0.0
        self._last_update = None
        self._cold_floor = False
        self.pi.freeze_output(0)
        self.learner.reset("demand off or no heating setpoint")

    def _sample_model(self, inp):
        previous = self._model_last_update
        dt = inp.now_min - previous if previous is not None else 0
        valid = (
            inp.valid
            and finite_float(inp.temp) is not None
            and finite_float(inp.outdoor) is not None
            and (previous is None or abs(dt - 1) <= 0.05)
        )
        self.model.minute(inp.valve_on, inp.temp, inp.outdoor, valid, max(0, dt))
        self._model_last_update = inp.now_min
        self._model_minute += 1
        self.tracker.record(
            self._model_minute, inp.temp, inp.valve_on, inp.outdoor, valid
        )
        if valid and (
            self._snapshot_at is None or inp.now_min - self._snapshot_at >= 15
        ):
            model = self.model.active()
            self.tracker.snapshot(
                self._model_minute,
                inp.temp,
                model.slab,
                inp.outdoor,
                model.theta,
                model.tau,
            )
            self._snapshot_at = inp.now_min

    def minute(self, inp: MinuteInputs) -> bool:
        """Process a minute and return whether the current PWM cycle must restart."""
        return self._evaluate(inp, advance=True)

    def _evaluate(self, inp, advance):
        """Evaluate readings; only an advancing call may count a new minute.

        A new minute samples the model, the tracker, PI integration and loss
        learning. Otherwise (the same minute again, or a plan) the setpoint,
        gate, cold floor and PI output are re-evaluated from the readings.
        """
        if self._last_inputs == inp:
            return self._restart
        new_minute = advance and self._processed_at != inp.now_min
        self._last_inputs = replace(inp)
        self._now_min = inp.now_min
        if advance:
            self._processed_at = inp.now_min
        self._restart = False
        if new_minute:
            self._sample_model(inp)
        temp, setpoint = finite_float(inp.temp), finite_float(inp.setpoint)
        previous_fault = self._fault
        self._fault = temp is None
        self._seen_temp |= temp is not None
        error = setpoint - temp if temp is not None and setpoint is not None else None
        changed = self._setpoint != setpoint
        if changed:
            self.learner.reset("setpoint changed")
            self._cycle_start = None
            self._restart = True
        self._setpoint = setpoint
        cold = error is not None and error > 0.5 and inp.demand
        if not (cold and self.mode == "predictive"):
            self._restart |= self._update_mode(inp.now_min)
        if cold and self.mode == "predictive":
            self.demoted_until_min = inp.now_min + 1440
            self._switch_mode("fallback")
            self._cold_floor = True
            self._restart = True
        if not inp.demand or setpoint is None:
            self._restart |= self._demand
            self._demand = False
            self._idle()
            return self._restart
        if not self._demand:
            self._restart = True
        self._demand = True
        ff = self.learner.feedforward(setpoint, finite_float(inp.outdoor))
        dt = inp.now_min - self._last_update if self._last_update is not None else 1
        if self._fault:
            # A temperature fault wins over a cold-room floor not yet planned.
            self._cold_floor = False
            self.pi.freeze_output(ff if self._seen_temp else 0)
            self.learner.reset("temperature unavailable")
            if not previous_fault:
                self.bank.clear()
                self._cycle_start = None
                self._restart = True
        elif self.mode == "predictive" and finite_float(inp.outdoor) is not None:
            self.bank.clear()
            self.pi.freeze_output(100 * self._on_minutes / self.bank.cycle_time)
            if previous_fault:
                self._cycle_start = None
                self._restart = True
        elif new_minute:
            self.pi.update(error, dt, ff)
        else:
            # A UI change can restart PWM between minute callbacks. Recompute
            # proportional/feed-forward output without inventing another minute.
            self.pi.p = self.pi.kp * error
            self.pi.i = self.pi.ki * self.pi.integral
            self.pi.output = clamp(ff + self.pi.p + self.pi.i)
        if not advance:
            return self._restart
        self._last_update = inp.now_min
        elapsed = (
            inp.now_min - self._cycle_start if self._cycle_start is not None else None
        )
        # At a boundary, sampling waits until plan() knows the new valve command,
        # matching v5's loss sample after _set_valve. The model used the old valve.
        if (
            not self._restart
            and elapsed is not None
            and 0 <= elapsed < self.bank.cycle_time - 0.05
        ):
            self._sample_loss(inp)
        return self._restart

    def _sample_loss(self, inp):
        if self._learned_at == inp.now_min:
            return
        self._learned_at = inp.now_min
        if self._fault or not inp.valid or not self._demand:
            self.learner.reset("invalid sample")
            return
        # A plan may read the clock just after the minute it completes.
        elapsed = max(0.0, inp.now_min - self._cycle_start)
        pwm = plan_pwm(
            100 * self._on_minutes / self.bank.cycle_time,
            60 * self.bank.cycle_time,
        )
        # plan_pwm has one symmetric minimum. Apply the two sides separately
        # so loss learning counts the same actuator command as the device.
        on_seconds = pwm.on_seconds
        if 0 < on_seconds < 60 * self.bank.min_on_time:
            on_seconds = 0
        elif 0 < pwm.off_seconds < 60 * self.settings.min_off_minutes:
            on_seconds = 60 * self.bank.cycle_time
        valve = elapsed + 0.05 < on_seconds / 60
        update = self.learner.sample(
            True, self._setpoint, inp.temp, finite_float(inp.outdoor), valve
        )
        if update is not None:
            old, mean_delta = update
            transfer_integral(self.pi, old, self.learner.k, mean_delta)

    def plan(self, inp: MinuteInputs) -> float:
        """Plan on-minutes at a boundary (PI banking retains fractional minutes).

        Only the first sample ever is processed as a minute here. Otherwise the
        plan uses the last processed minute's learning state and the current
        readings, and keeps a pending cold-room floor until it plans it.
        """
        self._evaluate(inp, advance=self._processed_at is None)
        if not inp.demand or finite_float(inp.setpoint) is None:
            self._idle()
            return 0
        if self._cold_floor:
            on = self.bank.cycle_time
        elif (
            self.mode == "predictive"
            and not self._fault
            and finite_float(inp.outdoor) is not None
        ):
            model = self.model.active()
            on, _ = plan_cycle(
                inp.temp,
                model.slab,
                inp.outdoor,
                inp.setpoint,
                model.theta,
                model.tau,
                self.bank.cycle_time,
                self.bank.min_on_time,
                min_off=self.settings.min_off_minutes,
            )
            self.pi.freeze_output(100 * on / self.bank.cycle_time)
        else:
            on = self.bank.plan(self.pi.output)
        self._on_minutes = on
        self._cycle_start = inp.now_min
        self._restart = False
        self._cold_floor = False
        # The boundary minute's loss sample waited for this valve command.
        self._sample_loss(replace(inp, now_min=self._processed_at))
        return on

    def forecasts(
        self, inp: MinuteInputs, cycle_elapsed: int, on_minutes: int
    ) -> dict[int, float | None]:
        """Cached v5 paths with exact linear T, S and outdoor corrections."""
        horizons = (30, 60, 120, 180, 240)
        values = dict.fromkeys(horizons)
        if finite_float(inp.temp) is None or finite_float(inp.outdoor) is None:
            return values
        model = self.model.active()
        cycle = self.settings.cycle_minutes
        heating = inp.demand and inp.setpoint is not None
        start = inp.now_min - cycle_elapsed if heating else None
        key = (start, on_minutes, inp.setpoint, heating, model.theta, model.tau)
        cache = self._forecast_cache
        age = inp.now_min - cache["time"] if cache else -1
        if cache is None or cache["key"] != key or age < 0 or age + 0.05 >= cycle:
            elapsed = round(cycle_elapsed) if heating else 0
            hold = (
                round(hold_duty(model.theta, inp.setpoint, inp.outdoor) * cycle)
                if heating
                else 0
            )
            if hold < self.bank.min_on_time:
                hold = 0
            plan = valve_plan(
                on_minutes if heating else 0, hold, cycle, elapsed + 240 + cycle
            )[elapsed:]
            path = forecast(
                inp.temp, model.slab, inp.outdoor, plan, model.theta, model.tau
            )
            slab, slabs = model.slab, [model.slab]
            for valve in plan:
                slab += (valve - slab) / model.tau
                slabs.append(slab)
            cache = dict(
                key=key,
                time=inp.now_min,
                temps=[inp.temp, *path],
                slabs=slabs,
                outdoor=inp.outdoor,
            )
            self._forecast_cache = cache
            age = 0
        index = round(age)
        alpha, beta, _ = model.theta
        r, q = 1 - beta, 1 - 1 / model.tau
        delta_t = inp.temp - cache["temps"][index]
        delta_s = model.slab - cache["slabs"][index]
        delta_out = inp.outdoor - cache["outdoor"]
        for horizon in horizons:
            coupling = (
                horizon * r ** (horizon - 1)
                if abs(r - q) < 1e-12
                else (r**horizon - q**horizon) / (r - q)
            )
            correction = (
                r**horizon * delta_t
                + alpha * delta_s * q * coupling
                + (1 - r**horizon) * delta_out
            )
            values[horizon] = round(cache["temps"][index + horizon] + correction, 1)
        return values

    def to_dict(self) -> dict:
        """Persist learned state; pending windows and running pulses are transient."""
        saved = datetime.now(timezone.utc)
        until = None
        if self.demoted_until_min is not None:
            until = saved + timedelta(minutes=self.demoted_until_min - self._now_min)
        return {
            **self.model.to_dict(),
            **self.tracker.to_dict(),
            "mode": self.mode,
            "demoted_until": until.isoformat() if until else None,
            "k_loss": self.learner.k,
            "integral": self.pi.integral,
            "k_updates": self.learner.updates,
            "saved_at": saved.isoformat(),
        }

    @classmethod
    def _restore(cls, settings, data, downtime_min):
        result = cls(settings)
        downtime = finite_float(downtime_min)
        if downtime is None:
            raise ValueError("invalid downtime")
        downtime = max(0, downtime)
        model, tracker = RoomModel.from_dict(data), AccuracyTracker.from_dict(data)
        k, integral = finite_float(data["k_loss"]), finite_float(data["integral"])
        updates = data.get("k_updates", 0)
        if (
            k is None
            or not 0 <= k <= 10
            or integral is None
            or not isinstance(updates, int)
            or updates < 0
        ):
            raise ValueError("invalid fallback state")
        if data["mode"] not in ("fallback", "predictive"):
            raise ValueError("invalid mode")
        saved = datetime.fromisoformat(data["saved_at"])
        if saved.tzinfo is None:
            raise ValueError("saved timestamp must be aware")
        demoted = data.get("demoted_until")
        if demoted is not None:
            until = datetime.fromisoformat(demoted)
            if until.tzinfo is None:
                raise ValueError("demotion timestamp must be aware")
            result.demoted_until_min = (until - saved).total_seconds() / 60 - downtime
        for candidate in model.models.values():
            candidate.slab *= math.exp(-downtime / candidate.tau)
        result.model, result.tracker = model, tracker
        result._model_last_update = 0
        result.pi.integral = integral
        result.learner.k, result.learner.updates = k, updates
        if settings.gate == "auto":
            result.mode = data["mode"]
        if result.demoted_until_min is not None and result.demoted_until_min > 0:
            result.mode = "fallback"
        return result

    @classmethod
    def from_dict(
        cls, settings: PredictiveSettings, data: dict, downtime_min: float
    ) -> PredictiveController:
        """Restore at monotonic minute zero, logging and discarding invalid state."""
        try:
            return cls._restore(settings, data, downtime_min)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            _LOGGER.warning("invalid saved predictive state; starting fresh: %s", exc)
            return cls(settings)

    @classmethod
    def from_appdaemon_room(
        cls, settings: PredictiveSettings, room: dict, downtime_min: float
    ) -> PredictiveController:
        """Import a v5 room; the service validates the enclosing version and path."""
        if not isinstance(room, dict) or "theta" not in room:
            raise ValueError("v5 model required; v4 rooms cannot be imported")
        data = dict(room)
        if data.get("mode") == "pi":
            data["mode"] = "fallback"
        data["saved_at"] = data.get("model_saved_at")
        try:
            return cls._restore(settings, data, downtime_min)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"invalid v5 room: {exc}") from exc
