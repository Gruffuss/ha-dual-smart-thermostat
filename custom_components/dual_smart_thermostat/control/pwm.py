"""PWM segment planning: turn a duty percentage into on/off switch timing.

Pure, side-effect-free helpers used by the PWM heater controller. Splitting the
planning out from the timer mechanics keeps the timing math unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PwmPlan:
    """On/off durations (seconds) for one PWM cycle."""

    on_seconds: float
    off_seconds: float

    @property
    def always_on(self) -> bool:
        return self.off_seconds <= 0.0 and self.on_seconds > 0.0

    @property
    def always_off(self) -> bool:
        return self.on_seconds <= 0.0


def plan_pwm(
    duty_pct: float,
    cycle_seconds: float,
    min_segment_seconds: float = 0.0,
) -> PwmPlan:
    """Split a PWM cycle into on/off segments for the given duty.

    ``on = duty% * cycle``; the remainder is off. To protect the actuator from
    chattering, a segment shorter than ``min_segment_seconds`` is collapsed:
    a too-short on-segment becomes fully off, and a too-short off-segment
    becomes fully on. ``min_segment_seconds`` is typically the configured
    ``min_cycle_duration``.
    """
    duty = max(0.0, min(100.0, duty_pct))
    cycle = max(0.0, cycle_seconds)

    on = duty / 100.0 * cycle
    off = cycle - on

    if min_segment_seconds > 0.0:
        if 0.0 < on < min_segment_seconds:
            # On-time too short to bother the relay -> stay off this cycle.
            return PwmPlan(on_seconds=0.0, off_seconds=cycle)
        if 0.0 < off < min_segment_seconds:
            # Off-time too short -> just stay on for the whole cycle.
            return PwmPlan(on_seconds=cycle, off_seconds=0.0)

    return PwmPlan(on_seconds=on, off_seconds=off)
