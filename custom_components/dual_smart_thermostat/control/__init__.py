"""Pure-logic heating control algorithms (HA-free, unit-testable).

These modules implement the duty-cycle math for the proportional heating control
modes (TPI today; AI Time Based later) and the PWM segment planning that turns a
duty percentage into on/off switch timing. They deliberately contain no Home
Assistant imports so the algorithms can be unit-tested in isolation.

The TPI algorithm is adapted from Better Thermostat
(https://github.com/KartoffelToby/better_thermostat), which in turn credits
versatile_thermostat.
"""
