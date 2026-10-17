"""PWM segment planning: turn a duty percentage into on/off switch timing.

Pure, side-effect-free helpers used by the PWM heater controller. Splitting the
planning out from the timer mechanics keeps the timing math unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass

# Segments come from a duty percentage, so one meant to equal the minimum can
# compute a few ulps short: 100 * 20 / 30 % of 1800 s leaves 599.9999999999998 s
# off. A segment is shorter than the minimum only by more than this.
_TOLERANCE_SECONDS = 1e-6


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
    min_off_seconds: float | None = None,
) -> PwmPlan:
    """Split a PWM cycle into on/off segments for the given duty.

    ``on = duty% * cycle``; the remainder is off. To protect the actuator from
    chattering, a segment shorter than ``min_segment_seconds`` is collapsed:
    a too-short on-segment becomes fully off, and a too-short off-segment
    becomes fully on. ``min_segment_seconds`` is typically the configured
    ``min_cycle_duration``. ``min_off_seconds`` gives the off side its own
    minimum; by default both sides use ``min_segment_seconds``. A segment
    within float rounding of its minimum is kept.
    """
    duty = max(0.0, min(100.0, duty_pct))
    cycle = max(0.0, cycle_seconds)
    min_off = min_segment_seconds if min_off_seconds is None else min_off_seconds

    on = duty / 100.0 * cycle
    off = cycle - on

    if 0.0 < on < min_segment_seconds - _TOLERANCE_SECONDS:
        # On-time too short to bother the relay -> stay off this cycle.
        return PwmPlan(on_seconds=0.0, off_seconds=cycle)
    if 0.0 < off < min_off - _TOLERANCE_SECONDS:
        # Off-time too short -> just stay on for the whole cycle.
        return PwmPlan(on_seconds=cycle, off_seconds=0.0)

    return PwmPlan(on_seconds=on, off_seconds=off)
