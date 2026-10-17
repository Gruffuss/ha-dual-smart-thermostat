"""Pure v5 slab identification, forecasting, planning and accuracy tracking."""

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


TAUS = (60, 90, 120, 150, 180, 240)
AC_ACTIVE = ("heat", "cool", "heat_cool", "dry", "fan_only", "auto")


class SlabRLS:
    """One lag candidate; regress 15 measured intervals, sharing endpoints."""

    def __init__(
        self, tau, theta0=(0.02, 0.001, 0.0), p0=(1e-3, 1e-6, 1e-4), lam=0.995
    ):
        self.tau, self.lam = tau, lam
        self.p0 = tuple(p0)
        self.slab, self.err, self.blocks = 0.0, 0.0, 0
        self.theta = tuple(theta0)
        self.P = [[p0[i] if i == j else 0.0 for j in range(3)] for i in range(3)]
        self.reset()

    def reset(self):
        self.start = self.previous = None
        self.n = 0
        self.sum_s = self.sum_delta = 0.0

    def step_slab(self, valve_on, dt):
        self.slab = clamp(self.slab + (int(valve_on) - self.slab) * dt / self.tau, 0, 1)

    def add_minute(self, temp, outdoor, valid):
        if not valid or finite_float(temp) is None or finite_float(outdoor) is None:
            self.reset()
            return False
        if self.previous is None:
            self.start = self.previous = temp
            return False
        self.sum_s += self.slab
        self.sum_delta += self.previous - outdoor
        self.previous = temp
        self.n += 1
        if self.n < 15:
            return False
        x = [self.sum_s / 15, -self.sum_delta / 15, 1.0]
        residual = (temp - self.start) / 15 - sum(
            x[i] * self.theta[i] for i in range(3)
        )
        px = [sum(self.P[i][j] * x[j] for j in range(3)) for i in range(3)]
        xp = [sum(x[i] * self.P[i][j] for i in range(3)) for j in range(3)]
        denominator = self.lam + sum(x[i] * px[i] for i in range(3))
        gain = [p / denominator for p in px]
        bounds = ((0, 0.2), (0, 0.02), (-0.02, 0.02))
        self.theta = tuple(
            clamp(self.theta[i] + gain[i] * residual, *bounds[i]) for i in range(3)
        )
        self.P = [
            [(self.P[i][j] - gain[i] * xp[j]) / self.lam for j in range(3)]
            for i in range(3)
        ]
        self._bound_covariance()
        self.err = (
            (0.95 * self.err + 0.05 * residual**2) if self.blocks else residual ** 2
        )
        self.blocks += 1
        self.reset()
        self.start = self.previous = temp
        return True

    def _bound_covariance(self):
        self.P = [
            [(self.P[i][j] + self.P[j][i]) / 2 for j in range(3)] for i in range(3)
        ]
        # D P D preserves symmetry and positive semidefiniteness.
        scales = [
            (
                math.sqrt(100 * self.p0[i] / self.P[i][i])
                if self.P[i][i] > 100 * self.p0[i]
                else 1
            )
            for i in range(3)
        ]
        self.P = [
            [self.P[i][j] * (scales[i] * scales[j]) for j in range(3)] for i in range(3)
        ]

    def alpha_rel_sd(self):
        alpha = self.theta[0]
        return (
            math.sqrt(max(0, self.err * self.P[0][0])) / alpha
            if alpha > 0
            else math.inf
        )


class RoomModel:
    def __init__(self, taus=TAUS):
        self.models = {tau: SlabRLS(tau) for tau in taus}

    def minute(self, valve_on, temp, outdoor, valid, dt=1.0):
        for model in self.models.values():
            model.step_slab(valve_on, dt)
            model.add_minute(temp, outdoor, valid)

    def active(self):
        return min(self.models.values(), key=lambda m: (m.err, abs(m.tau - 120), m.tau))

    def to_dict(self):
        result = {
            field: {str(tau): getattr(m, field) for tau, m in self.models.items()}
            for field in ("slab", "theta", "P", "err", "blocks")
        }
        result.update(active_tau=self.active().tau)
        # JSON has lists, so make the in-memory representation identical too.
        result["theta"] = {key: list(value) for key, value in result["theta"].items()}
        return result

    @classmethod
    def from_dict(cls, data):
        result = cls()
        for tau, model in result.models.items():
            key = str(tau)
            theta = data["theta"][key]
            matrix = data["P"][key]
            if (
                len(theta) != 3
                or len(matrix) != 3
                or any(len(row) != 3 for row in matrix)
            ):
                raise ValueError("invalid model dimensions")
            numbers = [
                *theta,
                *(v for row in matrix for v in row),
                data["slab"][key],
                data["err"][key],
            ]
            if any(finite_float(v) is None for v in numbers):
                raise ValueError("non-finite model state")
            model.theta = tuple(
                clamp(float(v), *bounds)
                for v, bounds in zip(theta, ((0, 0.2), (0, 0.02), (-0.02, 0.02)))
            )
            model.P = [[float(v) for v in row] for row in matrix]
            model._bound_covariance()
            model.slab = clamp(float(data["slab"][key]), 0, 1)
            model.err = max(0, float(data["err"][key]))
            blocks = data["blocks"]
            model.blocks = max(
                0, int(blocks[key] if isinstance(blocks, dict) else blocks)
            )
        return result


def forecast(temp, slab, outdoor, plan, theta, tau):
    alpha, beta, gamma = theta
    values = []
    for valve in plan:
        slab = clamp(slab + (valve - slab) / tau, 0, 1)
        temp += alpha * slab - beta * (temp - outdoor) + gamma
        values.append(temp)
    return values


def hold_duty(theta, setpoint, outdoor):
    alpha, beta, gamma = theta
    return (
        clamp((beta * (setpoint - outdoor) - gamma) / alpha, 0, 1) if alpha > 0 else 0
    )


def valve_plan(on, hold_on, cycle_time, horizon):
    return [
        int(minute < on if minute < cycle_time else minute % cycle_time < hold_on)
        for minute in range(horizon)
    ]


def plan_cycle(
    temp,
    slab,
    outdoor,
    setpoint,
    theta,
    tau,
    cycle_time=30,
    min_on=10,
    horizon=240,
    min_off=0,
):
    cycle_time, min_on = int(cycle_time), int(min_on)
    hold_on = round(hold_duty(theta, setpoint, outdoor) * cycle_time)
    if hold_on < min_on:
        hold_on = 0
    best = None
    all_cold = True
    for on in sorted({0, *range(min_on, cycle_time - int(min_off) + 1), cycle_time}):
        values = forecast(
            temp,
            slab,
            outdoor,
            valve_plan(on, hold_on, cycle_time, horizon),
            theta,
            tau,
        )
        all_cold = all_cold and max(values) < setpoint - 0.3
        cost = sum(
            (3 if t > setpoint else 1) * (t - setpoint) ** 2 * (0.2 if i < 30 else 1)
            for i, t in enumerate(values)
        )
        # Review Focus 3: a cold room may coast only if it will reach target.
        if on == 0 and temp < setpoint and max(values) < setpoint:
            cost = math.inf
        if best is None or cost < best[0]:
            best = (cost, on, values)
        if on == cycle_time:
            full = values
    return (cycle_time, full) if all_cold else (best[1], best[2])


class AccuracyTracker:
    """Fixed-size history and at most eight pending 15-minute snapshots."""

    def __init__(self):
        from collections import deque

        self.history = deque(maxlen=135)
        self.pending = deque(maxlen=9)
        self.mae60 = self.mae120 = 0.0
        self.persistence_mae60 = self.persistence_mae120 = 0.0
        self.n60 = self.n120 = 0
        self.n120_independent = 0
        self.last_independent_start = None

    def invalidate(self):
        self.pending.clear()

    def snapshot(self, now_min, temp, slab, outdoor, theta, tau):
        self.pending.append((now_min, temp, slab, outdoor, tuple(theta), tau))

    def record(self, now_min, temp, valve_on, outdoor, valid):
        if self.history and now_min != self.history[-1][0] + 1:
            self.invalidate()
        self.history.append((now_min, temp, int(valve_on), outdoor, valid))
        if not valid:
            self.invalidate()
        for start, t0, s0, out0, theta, tau in self.pending:
            age = now_min - start
            if age not in (60, 120):
                continue
            samples = [row for row in self.history if start < row[0] <= now_min]
            if len(samples) != age or not all(row[4] for row in samples):
                continue
            predicted = forecast(t0, s0, out0, [row[2] for row in samples], theta, tau)[
                -1
            ]
            error = abs(predicted - temp)
            for name, value in (
                (f"mae{age}", error),
                (f"persistence_mae{age}", abs(temp - t0)),
            ):
                seeded = getattr(self, f"n{age}") > 0
                setattr(
                    self,
                    name,
                    0.95 * getattr(self, name) + 0.05 * value if seeded else value,
                )
            name = f"n{age}"
            setattr(self, name, getattr(self, name) + 1)
            if age == 120 and (
                self.last_independent_start is None
                or start - self.last_independent_start >= 120
            ):
                self.n120_independent += 1
                self.last_independent_start = start
        while self.pending and now_min - self.pending[0][0] >= 120:
            self.pending.popleft()

    def to_dict(self):
        return {
            name: getattr(self, name)
            for name in (
                "mae60",
                "mae120",
                "n60",
                "n120",
                "persistence_mae60",
                "persistence_mae120",
                "n120_independent",
            )
        }

    @classmethod
    def from_dict(cls, data):
        result = cls()
        for name in result.to_dict():
            value = finite_float(data.get(name, 0))
            if value is None or value < 0:
                raise ValueError("invalid accuracy score")
            setattr(result, name, int(value) if name.startswith("n") else value)
        if "persistence_mae120" not in data:
            # Older v5 scores have no paired baseline; seed new paired windows.
            result.n60 = result.n120 = result.n120_independent = 0
        return result
