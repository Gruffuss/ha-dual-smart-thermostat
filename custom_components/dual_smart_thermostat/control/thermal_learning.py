"""Thermal learning for AI Time Based heating control.

Pure-Python EMA learners that estimate a room's effective heating power
(°C/min) and heat-loss rate (°C/min) from observed cycles, plus the control law
that maps the current error and learned heating power to a valve opening
fraction. Adapted from Better Thermostat's ``utils/thermal_learning.py`` and the
``heating_power_valve_position`` helper, reduced to HA-free logic that takes the
current time and a boolean ``is_heating`` so it can be unit-tested deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Learned-value bounds (realistic residential heating, from Better Thermostat).
DEFAULT_HEATING_POWER = 0.01  # °C/min
MIN_HEATING_POWER = 0.005
MAX_HEATING_POWER = 0.2
DEFAULT_HEAT_LOSS = 0.01
MIN_HEAT_LOSS = 0.001
MAX_HEAT_LOSS = 0.05

# EMA smoothing.
_BASE_ALPHA = 0.10
_ALPHA_MIN = 0.02
_ALPHA_MAX = 0.25

_TIMEOUT_MIN = 30  # plateau safety timeout (minutes)
_MIN_CYCLE_MINUTES = 1.0  # shortest cycle accepted

# Valve-position control-law constants.
_VALVE_A = 0.019
_VALVE_B = 0.946
_VALVE_MIN_THRESHOLD_TEMP_DIFF = 0.3
_VALVE_MIN_OPENING_LARGE_DIFF = 0.15
_VALVE_MIN_BASE = 0.05
_VALVE_MIN_SMALL_DIFF_THRESHOLD = 0.1
_VALVE_MIN_PROPORTIONAL_SLOPE = 0.5


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def ema_smooth(old: float, new: float, alpha: float) -> float:
    """Exponential moving average."""
    return old * (1.0 - alpha) + new * alpha


def compute_weight_factor(
    target_temp: float | None, min_target: float, max_target: float
) -> float:
    """Relative-position weight within the observed target range ([0.5, 1.5])."""
    temp_range = max(max_target - min_target, 0.1)
    relative_pos = (
        0.5 if target_temp is None else (target_temp - min_target) / temp_range
    )
    return clamp(0.5 + relative_pos, 0.5, 1.5)


def compute_env_factor(outdoor_temp: float | None, target_temp: float | None) -> float:
    """Environmental factor from the outdoor-to-setpoint gradient ([0.7, 1.3])."""
    if outdoor_temp is None or target_temp is None:
        return 1.0
    delta_env = max(target_temp - outdoor_temp, 0.1)
    return clamp(delta_env / 20.0, 0.7, 1.3)


def heating_power_valve_fraction(error: float, heating_power: float) -> float:
    """Map an error (target - current, °C) and learned heating power to 0-1 valve.

    ``valve = a * (error / heating_power) ** b`` with minimum-opening floors when
    heating is meaningfully needed. Returns 0 when the room is at or above target.
    """
    if error <= 0:
        return 0.0

    hp = clamp(float(heating_power), MIN_HEATING_POWER, MAX_HEATING_POWER)
    valve = _VALVE_A * (error / hp) ** _VALVE_B

    if error > _VALVE_MIN_THRESHOLD_TEMP_DIFF:
        valve = max(_VALVE_MIN_OPENING_LARGE_DIFF, valve)
    elif error >= _VALVE_MIN_SMALL_DIFF_THRESHOLD:
        min_valve = (
            _VALVE_MIN_BASE
            + (error - _VALVE_MIN_SMALL_DIFF_THRESHOLD) * _VALVE_MIN_PROPORTIONAL_SLOPE
        )
        valve = max(min_valve, valve)

    return clamp(valve, 0.0, 1.0)


@dataclass
class HeatingPowerTracker:
    """Learn effective heating power (°C/min) from heat -> peak cycles."""

    heating_power: float = DEFAULT_HEATING_POWER
    min_target: float = 18.0
    max_target: float = 21.0
    start_temp: float | None = None
    start_ts: datetime | None = None
    end_temp: float | None = None  # peak temperature after heating stops
    end_ts: datetime | None = None
    _prev_heating: bool = False

    def update(
        self,
        cur_temp: float,
        is_heating: bool,
        now: datetime,
        *,
        target_temp: float | None = None,
        outdoor_temp: float | None = None,
    ) -> bool:
        """Process one reading; return True if ``heating_power`` changed."""
        if is_heating and not self._prev_heating:
            # Heating started.
            self.start_temp = cur_temp
            self.start_ts = now
            self.end_temp = None
            self.end_ts = None
        elif (
            not is_heating
            and self._prev_heating
            and self.start_temp is not None
            and self.end_temp is None
        ):
            # Heating stopped: candidate cycle end.
            self.end_temp = cur_temp
            self.end_ts = now
        elif (
            not is_heating
            and self.start_temp is not None
            and self.end_temp is not None
            and cur_temp > self.end_temp
        ):
            # Temperature still rising after heating stopped: track the peak.
            self.end_temp = cur_temp
            self.end_ts = now

        power_changed = self._maybe_finalize(
            cur_temp, now, target_temp=target_temp, outdoor_temp=outdoor_temp
        )

        if target_temp is not None:
            self.min_target = min(self.min_target, target_temp)
            self.max_target = max(self.max_target, target_temp)

        self._prev_heating = is_heating
        return power_changed

    def _maybe_finalize(
        self,
        cur_temp: float,
        now: datetime,
        *,
        target_temp: float | None,
        outdoor_temp: float | None,
    ) -> bool:
        finalize = False
        if (
            self.start_temp is not None
            and self.end_temp is not None
            and cur_temp < self.end_temp
        ):
            finalize = True
        elif self.end_ts is not None and (now - self.end_ts) > timedelta(
            minutes=_TIMEOUT_MIN
        ):
            finalize = True

        if not finalize:
            return False

        power_changed = False
        temp_diff = (
            self.end_temp - self.start_temp
            if self.end_temp is not None and self.start_temp is not None
            else 0.0
        )
        duration_min = (
            (self.end_ts - self.start_ts).total_seconds() / 60.0
            if self.end_ts is not None and self.start_ts is not None
            else 0.0
        )

        if duration_min >= _MIN_CYCLE_MINUTES and temp_diff > 0:
            weight = compute_weight_factor(
                target_temp, self.min_target, self.max_target
            )
            env = compute_env_factor(outdoor_temp, target_temp)
            alpha = clamp(_BASE_ALPHA * weight * env, _ALPHA_MIN, _ALPHA_MAX)

            heating_rate = temp_diff / duration_min
            old_power = self.heating_power
            new_power = clamp(
                ema_smooth(old_power, heating_rate, alpha),
                MIN_HEATING_POWER,
                MAX_HEATING_POWER,
            )
            self.heating_power = round(new_power, 4)
            power_changed = self.heating_power != old_power

        # Reset for the next cycle (even when discarded).
        self.start_temp = None
        self.end_temp = None
        self.start_ts = None
        self.end_ts = None
        return power_changed


@dataclass
class HeatLossTracker:
    """Learn heat-loss rate (°C/min) during idle (non-heating) periods."""

    heat_loss_rate: float = DEFAULT_HEAT_LOSS
    start_temp: float | None = None
    start_ts: datetime | None = None
    end_temp: float | None = None  # lowest observed temperature
    end_ts: datetime | None = None
    _prev_heating: bool = False

    def update(
        self,
        cur_temp: float,
        is_heating: bool,
        now: datetime,
        *,
        window_open: bool = False,
    ) -> bool:
        """Process one reading; return True if ``heat_loss_rate`` changed."""
        if window_open:
            self._reset()
            self._prev_heating = is_heating
            return False

        if not is_heating:
            if self.start_temp is None:
                self.start_temp = cur_temp
                self.start_ts = now
                self.end_temp = cur_temp
                self.end_ts = now
            elif self.end_temp is None or cur_temp < self.end_temp:
                self.end_temp = cur_temp
                self.end_ts = now

        loss_changed = False
        if is_heating and self.start_temp is not None:
            loss_changed = self._finalize()

        self._prev_heating = is_heating
        return loss_changed

    def _finalize(self) -> bool:
        loss_changed = False
        temp_drop = (
            self.start_temp - self.end_temp
            if self.start_temp is not None and self.end_temp is not None
            else 0.0
        )
        duration_min = (
            (self.end_ts - self.start_ts).total_seconds() / 60.0
            if self.end_ts is not None and self.start_ts is not None
            else 0.0
        )

        if duration_min >= _MIN_CYCLE_MINUTES and temp_drop > 0:
            loss_rate = temp_drop / duration_min
            old_loss = self.heat_loss_rate
            new_loss = clamp(
                ema_smooth(old_loss, loss_rate, _BASE_ALPHA),
                MIN_HEAT_LOSS,
                MAX_HEAT_LOSS,
            )
            self.heat_loss_rate = round(new_loss, 5)
            loss_changed = self.heat_loss_rate != old_loss

        self._reset()
        return loss_changed

    def _reset(self) -> None:
        self.start_temp = None
        self.start_ts = None
        self.end_temp = None
        self.end_ts = None
