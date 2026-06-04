"""Periodic valve maintenance: exercise a switch so the valve doesn't seize.

Runs even when heating is off (the point is to cycle the valve during summer).
On each scheduled run it pulses the switch on/off twice, then restores the prior
state. Normal control is suspended for the duration via the supplied callbacks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging
from random import random

from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import DOMAIN as HA_DOMAIN, HomeAssistant
from homeassistant.helpers.event import async_track_point_in_time
import homeassistant.util.dt as dt_util

from ..control.valve_maintenance import (
    initial_maintenance_time,
    max_jitter_hours,
    next_maintenance_time,
)

_LOGGER = logging.getLogger(__name__)


class ValveMaintenanceManager:
    """Schedule and run periodic anti-stick cycles on a switch entity."""

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        interval_days: float,
        suspend_control: Callable[[bool], None],
        segment_seconds: float = 30.0,
        cycles: int = 2,
    ) -> None:
        self.hass = hass
        self.entity_id = entity_id
        self._interval_days = interval_days
        self._suspend_control = suspend_control
        self._segment_seconds = segment_seconds
        self._cycles = cycles
        self._cancel_timer: Callable[[], None] | None = None
        self._running = False

    def schedule_initial(self) -> None:
        """Schedule the first maintenance run after startup."""
        when = initial_maintenance_time(dt_util.utcnow(), self._interval_days)
        self._schedule_at(when)

    def _schedule_next(self) -> None:
        jitter = random() * max_jitter_hours(self._interval_days)
        when = next_maintenance_time(dt_util.utcnow(), self._interval_days, jitter)
        self._schedule_at(when)

    def _schedule_at(self, when) -> None:
        self.cancel()
        _LOGGER.debug("Scheduling valve maintenance for %s at %s", self.entity_id, when)
        self._cancel_timer = async_track_point_in_time(self.hass, self._async_run, when)

    def cancel(self) -> None:
        """Cancel the pending maintenance timer."""
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

    async def _async_run(self, *_) -> None:
        try:
            await self.async_run_cycle()
        finally:
            self._schedule_next()

    async def async_run_cycle(self) -> None:
        """Run the open/close exercise and restore the prior switch state."""
        state = self.hass.states.get(self.entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.debug("Skipping valve maintenance; %s unavailable", self.entity_id)
            return

        prior_on = state.state == STATE_ON
        _LOGGER.info("Running valve maintenance for %s", self.entity_id)

        self._running = True
        self._suspend_control(True)
        try:
            for _ in range(self._cycles):
                await self._switch(SERVICE_TURN_ON)
                await asyncio.sleep(self._segment_seconds)
                await self._switch(SERVICE_TURN_OFF)
                await asyncio.sleep(self._segment_seconds)
            # Restore prior state.
            await self._switch(SERVICE_TURN_ON if prior_on else SERVICE_TURN_OFF)
        finally:
            self._running = False
            self._suspend_control(False)
            _LOGGER.info("Valve maintenance finished for %s", self.entity_id)

    @property
    def running(self) -> bool:
        return self._running

    async def _switch(self, service: str) -> None:
        try:
            await self.hass.services.async_call(
                HA_DOMAIN,
                service,
                {ATTR_ENTITY_ID: self.entity_id},
                blocking=True,
            )
        except Exception as e:  # noqa: BLE001
            _LOGGER.error(
                "Valve maintenance %s failed for %s: %s", service, self.entity_id, e
            )

    @property
    def interval(self) -> timedelta:
        return timedelta(days=self._interval_days)
