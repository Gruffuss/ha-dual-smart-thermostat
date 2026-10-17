"""V4 PI fallback, duty banking and heat-loss feed-forward learning."""

from __future__ import annotations

import math


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def finite_float(value) -> float | None:
    """Unavailable, malformed and non-finite readings are not temperatures."""
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


class DutyBank:
    """Carry short heating requests forward until a useful pulse is owed."""

    def __init__(self, cycle_time: float, min_on_time: float):
        self.cycle_time = cycle_time
        self.min_on_time = min_on_time
        self.bank = 0.0

    def plan(self, output_pct: float) -> float:
        self.bank = min(
            self.cycle_time, self.bank + clamp(output_pct) / 100 * self.cycle_time
        )
        on = min(self.bank, self.cycle_time) if self.bank >= self.min_on_time else 0.0
        self.bank -= on
        return on

    def clear(self) -> None:
        self.bank = 0.0


class PIController:
    def __init__(self, kp: float, ki: float, i_max: float = 30.0):
        self.kp = kp
        self.ki = ki
        self.i_max = i_max
        self.integral = 0.0
        self.p = self.i = self.output = 0.0

    def update(self, error: float, dt: float, ff: float) -> float:
        self.p = self.kp * error
        candidate = self.integral + error * clamp(dt, 0.5, 5.0)
        candidate_output = ff + self.p + self.ki * candidate
        if not (
            (candidate_output > 100 and error > 0)
            or (candidate_output < 0 and error < 0)
        ):
            self.integral = candidate
        if self.ki > 0:
            self.integral = clamp(
                self.integral, -self.i_max / self.ki, self.i_max / self.ki
            )
        self.i = self.ki * self.integral
        self.output = clamp(ff + self.p + self.i)
        return self.output

    def freeze_output(self, ff: float) -> float:
        self.p = self.i = 0.0
        self.output = clamp(ff)
        return self.output


class LossLearner:
    """Non-overlapping steady-state windows estimate duty per degree of loss."""

    def __init__(
        self,
        k: float,
        window: int = 180,
        band: float = 0.3,
        alpha: float = 0.2,
        k_max: float = 10.0,
    ):
        self.k = k
        self.window = window
        self.band = band
        self.alpha = alpha
        self.k_max = k_max
        self.updates = 0
        self.reset("waiting")

    def feedforward(self, setpoint: float, outdoor: float | None) -> float:
        return 0.0 if outdoor is None else clamp(self.k * (setpoint - outdoor))

    def reset(self, reason: str) -> None:
        self.n = 0
        self.valve_on_count = 0
        self.sum_dT = 0.0
        self.room_start = self.start_setpoint = None
        self.status = f"idle: {reason}"

    def sample(
        self,
        valid: bool,
        setpoint: float,
        room: float,
        outdoor: float | None,
        valve_on: bool,
    ) -> tuple[float, float] | None:
        if not valid or outdoor is None:
            self.reset("invalid sample" if not valid else "outdoor unavailable")
            return None
        if self.n and setpoint != self.start_setpoint:
            self.reset("setpoint changed")
            return None
        # Decimal sensor readings can subtract a few ulps above the boundary.
        if abs(setpoint - room) > self.band + 1e-9:
            self.reset("outside band")
            return None
        if self.n == 0:
            self.room_start = room
            self.start_setpoint = setpoint
        self.n += 1
        self.valve_on_count += int(valve_on)
        self.sum_dT += setpoint - outdoor
        self.status = f"collecting {self.n}/{self.window}"
        if self.n < self.window:
            return None
        mean_dT = self.sum_dT / self.n
        if abs(room - self.room_start) > 0.2 + 1e-9 or mean_dT < 5 - 1e-9:
            self.reset("room drift or small delta T")
            return None
        k_old = self.k
        k_obs = (100 * self.valve_on_count / self.n) / mean_dT
        self.k = clamp(self.k + self.alpha * (k_obs - self.k), 0, self.k_max)
        self.updates += 1
        self.reset("window complete")
        return k_old, mean_dT


def transfer_integral(
    pi: PIController, k_old: float, k_new: float, mean_dT: float
) -> None:
    if pi.ki > 0:
        pi.integral -= (k_new - k_old) * mean_dT / pi.ki
        pi.i = pi.ki * pi.integral
