"""PWM heater controller: drive an on/off switch from a duty percentage.

Turns the duty cycle produced by a proportional control mode (TPI today, AI
later) into timed on/off switching of the heater switch. The duty-cycle math and
segment planning live in the pure ``control`` package; this class owns only the
Home Assistant timer mechanics and switch callbacks.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_point_in_time
import homeassistant.util.dt as dt_util

from ..control.pwm import plan_pwm

_LOGGER = logging.getLogger(__name__)


class PwmHeaterController:
    """Run a PWM duty cycle on a heater switch using HA timers.

    The controller recomputes the duty at every cycle boundary via the supplied
    ``compute_duty`` callback (which reads the current temperatures), plans the
    on/off split, switches accordingly, and re-arms a timer for the next
    boundary. It is started/stopped by the owning device in response to mode,
    opening and floor-protection changes.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        cycle_duration: timedelta,
        min_cycle_duration: timedelta | None,
        compute_duty: Callable[[], float],
        turn_on_callback: Callable[[], Awaitable[None]],
        turn_off_callback: Callable[[], Awaitable[None]],
    ) -> None:
        self.hass = hass
        self.entity_id = entity_id
        self._cycle_seconds = max(1.0, cycle_duration.total_seconds())
        self._min_segment_seconds = (
            min_cycle_duration.total_seconds() if min_cycle_duration else 0.0
        )
        self._compute_duty = compute_duty
        self._turn_on = turn_on_callback
        self._turn_off = turn_off_callback

        self._running = False
        self._cancel_timer: Callable[[], None] | None = None
        self._pending_off_seconds = 0.0
        self._last_duty: float | None = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_duty(self) -> float | None:
        return self._last_duty

    async def async_start(self) -> None:
        """Start the PWM loop (no-op if already running)."""
        if self._running:
            return
        _LOGGER.debug("Starting PWM loop for %s", self.entity_id)
        self._running = True
        await self._async_begin_cycle()

    async def async_stop(self) -> None:
        """Stop the PWM loop and turn the switch off."""
        if not self._running and self._cancel_timer is None:
            return
        _LOGGER.debug("Stopping PWM loop for %s", self.entity_id)
        self._running = False
        self._last_duty = None
        self._clear_timer()
        await self._turn_off()

    def cancel(self) -> None:
        """Cancel timers without issuing switch calls (for teardown)."""
        self._running = False
        self._clear_timer()

    async def _async_begin_cycle(self, *_) -> None:
        if not self._running:
            return

        duty = max(0.0, min(100.0, float(self._compute_duty())))
        self._last_duty = duty
        plan = plan_pwm(duty, self._cycle_seconds, self._min_segment_seconds)

        _LOGGER.debug(
            "PWM %s: duty=%.1f%% on=%.0fs off=%.0fs",
            self.entity_id,
            duty,
            plan.on_seconds,
            plan.off_seconds,
        )

        if plan.always_off:
            await self._turn_off()
            self._schedule(self._async_begin_cycle, self._cycle_seconds)
        elif plan.always_on:
            await self._turn_on()
            self._schedule(self._async_begin_cycle, self._cycle_seconds)
        else:
            await self._turn_on()
            self._pending_off_seconds = plan.off_seconds
            self._schedule(self._async_turn_off_segment, plan.on_seconds)

    async def _async_turn_off_segment(self, *_) -> None:
        if not self._running:
            return
        await self._turn_off()
        self._schedule(self._async_begin_cycle, self._pending_off_seconds)

    def _schedule(self, callback: Callable, seconds: float) -> None:
        self._clear_timer()
        when = dt_util.utcnow() + timedelta(seconds=max(0.0, seconds))
        self._cancel_timer = async_track_point_in_time(self.hass, callback, when)

    def _clear_timer(self) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
