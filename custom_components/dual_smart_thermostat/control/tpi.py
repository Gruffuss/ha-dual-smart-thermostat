"""Time Proportional Integrator (TPI) duty-cycle computation.

Adapted from Better Thermostat's ``utils/calibration/tpi.py`` (which credits
versatile_thermostat). Reduced to a pure, stateless function that returns a duty
cycle in percent for a heating valve given the current/target/outdoor
temperatures. The duty is applied to an on/off switch as a PWM cycle elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass

# Defaults match Better Thermostat / versatile_thermostat.
DEFAULT_COEF_INT = 0.6
DEFAULT_COEF_EXT = 0.01
DEFAULT_OVERSHOOT_THRESHOLD = 0.3


@dataclass
class TpiParams:
    """Tunable TPI coefficients.

    coef_int:
        Weight on the internal error ``target - current`` (the dominant term).
    coef_ext:
        Weight on the external gradient ``target - outdoor`` (compensates for
        heat loss). Ignored when no outdoor temperature is available.
    overshoot_threshold:
        When the room is more than this many °C above target, force the duty to
        0 so an overshooting room stops heating immediately.
    """

    coef_int: float = DEFAULT_COEF_INT
    coef_ext: float = DEFAULT_COEF_EXT
    overshoot_threshold: float = DEFAULT_OVERSHOOT_THRESHOLD


def compute_tpi_duty(
    current_temp: float | None,
    target_temp: float | None,
    outdoor_temp: float | None,
    params: TpiParams,
) -> float:
    """Return the heating duty cycle in percent (0-100).

    ``duty = coef_int * (target - current) + coef_ext * (target - outdoor)``,
    scaled to percent and clamped to ``[0, 100]``. Returns 0 when temperatures
    are missing or the room has overshot the target by more than
    ``overshoot_threshold``.
    """
    if current_temp is None or target_temp is None:
        return 0.0

    error = float(target_temp) - float(current_temp)

    # Overshoot cutoff: room is well above target -> stop heating.
    if error < -params.overshoot_threshold:
        return 0.0

    duty = params.coef_int * error
    if outdoor_temp is not None:
        duty += params.coef_ext * (float(target_temp) - float(outdoor_temp))

    duty *= 100.0
    return max(0.0, min(100.0, duty))
